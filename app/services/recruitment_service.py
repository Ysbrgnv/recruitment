import logging
import re
import time
import copy

from app.persistence.app_state import set_review, get_task, update_task
from app.core.errors import AIUnavailableError, HHUnavailableError, UserFacingError
from app.core import settings
from app.integrations.hh.hh_client import fetch_resume_details, fetch_vacancy_details, search_resumes
from app.integrations.ai.nano_client import evaluate_one_resume, evaluation_to_text, generate_vacancy_search_plan, generate_xray_query
from app.services.recruitment_data import (assess_candidate_leadership_v2, classify_candidate_level, resume_filter_result, select_current_or_latest_experience, vacancy_requirements)
from app.core.settings import (
    MAX_CANDIDATE_POOL, POOL_EXTRA, POOL_MULTIPLIER,
    SEARCH_MAX_PAGES, SEARCH_PER_PAGE, SEARCH_SCAN_LIMIT,
    validate_nano_settings,
)
from app.persistence.tracking_service import normalize, reset_history_cache, was_sent_to_ai

logger = logging.getLogger(__name__)
ROLE_STOP_WORDS = {
    "senior",
    "middle",
    "junior",
    "специалист",
    "менеджер",
    "manager",
    "инженер",
    "engineer",
    "developer",
    "разработчик",
}
PRESALES_MARKERS = (
    "pre sale",
    "pre sales",
    "presale",
    "presales",
    "пресейл",
    "пре сейл",
)
TELEMARKETING_CONTRACT_MARKERS = (
    "телемаркетинг",
    "телемаркетинга",
    "телемаркетингу",
    "телемаркетолог",
    "оператор исходящих звонков",
    "оператор исходящего обзвона",
    "специалист телефонных продаж",
    "менеджер телефонных продаж",
)
TELEMARKETING_ADJACENT_ROLE_PATTERNS = (
    r"(?<!\w)(?:call|колл|кол)\s+центр",
    r"(?<!\w)контакт\s+центр",
    r"(?<!\w)менеджер\s+по\s+продаж",
    r"(?<!\w)специалист\s+по\s+продаж",
    r"(?<!\w)менеджер\s+отдела\s+развития\s+продаж",
    r"(?<!\w)телефонн\w*\s+продаж",
)
TELEMARKETING_DOMAIN_EXCLUSIONS = (
    "подбор персонала",
    "рекрутер",
    "кандидат",
    "собеседован",
    "взыскан",
    "задолжен",
    "заемщик",
    "просроч",
    "административн",
    "исходящая корреспонденц",
    "туризм",
)


def _normalize_role_text(value):
    value = str(value or "").casefold().replace("ё", "е")
    value = re.sub(r"[‐‑‒–—−_/\\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def _leading_role_variants(query):
    query = str(query or "").lstrip()
    if not query.startswith("("): return []
    depth = 0; quoted = False
    for index, char in enumerate(query):
        if char == '"': quoted = not quoted
        elif not quoted and char == "(": depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                return [_normalize_role_text(value) for value in re.findall(r'"([^"\r\n]+)"', query[1:index])]
    return []

def _professional_role_names(resume):
    values = []
    for role in resume.get("professional_roles") or []:
        value = role.get("name") if isinstance(role, dict) else role if isinstance(role, str) else None
        if value: values.append(value)
    return values

def _normalized_start(value):
    values = [int(item) for item in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((values + [0, 0, 0])[:3])

def _current_or_latest_experience(resume):
    experience = [item for item in resume.get("experience") or [] if isinstance(item, dict)]
    current = [item for item in experience if item.get("end") in (None, "")]
    return current or ([max(experience, key=lambda item: _normalized_start(item.get("start")))] if experience else [])

def _experience_position(item):
    return item.get("position") or item.get("name") or item.get("title") or ""

def _current_or_latest_positions(resume):
    return [_experience_position(item) for item in _current_or_latest_experience(resume)]

def _role_texts(resume):
    return [resume.get("title") or "", *_professional_role_names(resume), *_current_or_latest_positions(resume)]

def _role_tokens(value):
    return {token for token in re.findall(r"\w+", _normalize_role_text(value), flags=re.UNICODE) if len(token) >= 4 and token not in ROLE_STOP_WORDS}

def _is_telemarketing_contract(variants):
    return any(
        "телемаркет" in variant
        or any(marker in variant for marker in TELEMARKETING_CONTRACT_MARKERS)
        or ("исходящ" in variant and ("звон" in variant or "обзвон" in variant))
        or ("телефонн" in variant and "продаж" in variant)
        for variant in variants
    )

def _matches_telemarketing_role(text):
    normalized = _normalize_role_text(text)
    return bool(
        "телемаркет" in normalized
        or ("исходящ" in normalized and ("звон" in normalized or "обзвон" in normalized))
        or ("телефонн" in normalized and "продаж" in normalized)
    )

def _has_telemarketing_adjacent_role(value):
    normalized = _normalize_role_text(value)
    return any(re.search(pattern, normalized, flags=re.UNICODE) for pattern in TELEMARKETING_ADJACENT_ROLE_PATTERNS)

def _has_telemarketing_domain_exclusion(position, description):
    combined = _normalize_role_text(f"{position} {description}")
    return any(marker in combined for marker in TELEMARKETING_DOMAIN_EXCLUSIONS)

def _has_telemarketing_duty_evidence(description):
    normalized = _normalize_role_text(description)
    return bool(
        ("исходящ" in normalized and "звон" in normalized)
        or ("исходящ" in normalized and "обзвон" in normalized)
        or ("обзвон" in normalized and ("холодн" in normalized or "тепл" in normalized) and "баз" in normalized)
        or ("телефонн" in normalized and "звон" in normalized and "цел" in normalized and "продаж" in normalized)
        or ("телефонн" in normalized and "продаж" in normalized)
        or ("продаж" in normalized and "по телефон" in normalized)
        or ("холодн" in normalized and "звон" in normalized)
    )

def _matches_telemarketing_experience(resume):
    fallback_adjacent_role = any(
        _has_telemarketing_adjacent_role(value)
        for value in [resume.get("title") or "", *_professional_role_names(resume)]
    )
    for item in _current_or_latest_experience(resume):
        position = _experience_position(item)
        description = item.get("description") or ""
        if _has_telemarketing_domain_exclusion(position, description):
            continue
        adjacent_role = (
            _has_telemarketing_adjacent_role(position)
            if position.strip()
            else fallback_adjacent_role
        )
        if not adjacent_role:
            continue
        if _has_telemarketing_duty_evidence(description):
            return True
    return False

def _role_text_matches(text, variants):
    normalized = _normalize_role_text(text)
    if not normalized: return False
    presales_contract = any(any(marker in variant for marker in PRESALES_MARKERS) for variant in variants)
    if presales_contract and any(marker in normalized for marker in PRESALES_MARKERS): return True
    if _is_telemarketing_contract(variants) and _matches_telemarketing_role(normalized): return True
    for variant in variants:
        if normalized == variant: return True
        text_tokens = _role_tokens(normalized); variant_tokens = _role_tokens(variant)
        overlap = text_tokens & variant_tokens; denominator = min(len(text_tokens), len(variant_tokens))
        if len(overlap) >= 2 and denominator and len(overlap) / denominator >= 0.5: return True
    return False

def _resume_role_matches_query(resume, query):
    variants = _leading_role_variants(query)
    if not variants:
        return False
    if any(_role_text_matches(text, variants) for text in _role_texts(resume)):
        return True
    return _is_telemarketing_contract(variants) and _matches_telemarketing_experience(resume)

CLEAN_POOL_SCAN_MULTIPLIER = 2
_PROFESSIONAL_MISMATCH_TITLES = {
    "business analyst", "бизнес-аналитик", "system analyst", "системный аналитик",
    "head of analytics", "руководитель аналитики", "director of data", "директор по данным",
}


def _resume_professional_text(resume, current_role):
    values=[resume.get("title"),current_role.get("title"),current_role.get("responsibilities")]
    values.extend(resume.get("skill_set") or [])
    for role in resume.get("professional_roles") or []:
        values.append(role.get("name") if isinstance(role,dict) else role)
    for item in resume.get("experience") or []:
        if isinstance(item,dict):values.extend([item.get("position"),item.get("description")])
    return " ".join(str(value) for value in values if value).casefold()


def _profile_terms(profile):
    terms=[]
    for title in profile.get("accepted_role_titles") or []:terms.append(title.casefold())
    for function in profile.get("target_functions") or []:terms.append(function.casefold())
    for concept in profile.get("mandatory_search_concepts") or []:
        if isinstance(concept,dict):terms.extend(str(x).casefold() for x in concept.get("alternatives") or [])
    return [term for term in terms if len(term)>2]


def professional_prefilter(profile,resume,current_role):
    if not isinstance(profile,dict):return {"decision":"keep","strength":"unknown","evidence":["Legacy task without VacancySearchProfile."]}
    text=_resume_professional_text(resume,current_role);title=str(current_role.get("title") or "").casefold()
    matched=[term for term in _profile_terms(profile) if term in text]
    role_family=str(profile.get("role_family") or "").casefold()
    presales=any(x in role_family for x in ("presale","pre-sale","пресейл"))
    functional_presales=any(x in text for x in ("discovery","выявлен","квалификац","демонстрац","demo","solution demo","poc","пилот","предпродаж","presale","pre-sale","пресейл","sales engineer","solution consultant"))
    if presales and title in _PROFESSIONAL_MISMATCH_TITLES and not functional_presales:
        return {"decision":"professional_mismatch","strength":"insufficient","evidence":[f"Current role is outside Presales: {current_role.get('title')}"]}
    if matched or (presales and functional_presales):
        return {"decision":"keep","strength":"strong","evidence":matched[:3] or ["Resume contains Presales functional evidence."]}
    return {"decision":"keep","strength":"unknown","evidence":["Insufficient evidence for deterministic professional exclusion."]}


def _pool_filter_decision(profile,resume):
    current_role=select_current_or_latest_experience(resume)
    professional=professional_prefilter(profile,resume,current_role)
    candidate=classify_candidate_level(current_role)
    vacancy_level=((profile or {}).get("leadership_intent") or {}).get("level","unknown")
    if professional["decision"]=="professional_mismatch":
        return False,"professional_mismatch",vacancy_level,candidate,professional["evidence"]
    candidate_level=candidate["level"]
    if vacancy_level=="individual_contributor" and candidate_level=="leadership":
        return False,"leadership_mismatch",vacancy_level,candidate,candidate["evidence"]
    if vacancy_level=="leadership" and candidate_level=="individual_contributor":
        return False,"level_mismatch",vacancy_level,candidate,candidate["evidence"]
    return True,None,vacancy_level,candidate,professional["evidence"]+candidate["evidence"]


def _progress_payload(metrics, target_pool, requested_count):
    return {**metrics, "target_pool": target_pool, "requested_count": requested_count}


def _build_candidate_result(vacancy, resume, number, notes):
    evaluation = evaluate_one_resume(vacancy, resume, number, notes)
    return {
        "number": number,
        "resume_id": str(resume.get("id") or ""),
        "url": resume.get("alternate_url") or "",
        "evaluation": evaluation,
        "evaluation_text": evaluation_to_text(evaluation),
    }


def _search_candidate_pool_v1(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile=None, pool_exclusions=None, existing_pool=None, checkpoint=None):
    reset_history_cache()
    candidates = list(existing_pool or [])
    pool_exclusions = pool_exclusions if isinstance(pool_exclusions,dict) else {}
    seen = {str(item[0]) for item in candidates if item} | set(pool_exclusions)
    scan_budget = SEARCH_SCAN_LIMIT if not profile else max(SEARCH_SCAN_LIMIT,min(SEARCH_SCAN_LIMIT*CLEAN_POOL_SCAN_MULTIPLIER,target_pool*8))
    for page in range(SEARCH_MAX_PAGES):
        if metrics["scanned"] >= scan_budget or len(candidates) >= target_pool:
            break
        params = {"text": query, "per_page": SEARCH_PER_PAGE, "page": page}
        if requirements.get("area_id"):
            params["area"] = requirements["area_id"]
        data = search_resumes(params)
        items = data.get("items", [])
        metrics["search_items"] += len(items)
        progress("search", f"Поиск HH: страница {page + 1}", _progress_payload(metrics, target_pool, requested_count))
        for item in items:
            if metrics["scanned"] >= scan_budget or len(candidates) >= target_pool:
                break
            resume_id = str(item.get("id") or "")
            if not resume_id or resume_id in seen:
                continue
            seen.add(resume_id)
            metrics["scanned"] += 1
            if was_sent_to_ai(resume_id, vacancy_id):
                metrics["already_sent"] += 1
                continue
            item_area = str((item.get("area") or {}).get("id") or "")
            if requirements.get("area_id") and item_area and item_area != requirements["area_id"]:
                metrics["excluded_city"] += 1
                continue
            try:
                resume = fetch_resume_details(resume_id)
            except HHUnavailableError:
                raise
            except Exception:
                metrics["load_failed"] += 1
                logger.exception("Не удалось загрузить резюме %s", resume_id)
                continue
            accepted, notes, reason = resume_filter_result(resume, requirements)
            if not accepted:
                metric = {
                    "city": "excluded_city",
                    "experience": "excluded_experience",
                    "leadership": "excluded_leadership",
                }.get(reason)
                if metric is None:
                    raise ValueError(f"Unknown resume filter rejection reason: {reason!r}")
                metrics[metric] = metrics.get(metric, 0) + 1
                continue
            if not _resume_role_matches_query(resume, query):
                metrics["excluded_role_mismatch"] = metrics.get("excluded_role_mismatch", 0) + 1
                continue
            if was_sent_to_ai(resume_id, vacancy_id):
                metrics["already_sent"] += 1
                continue
            if profile:
                keep,filter_reason,vacancy_level,candidate_level,evidence=_pool_filter_decision(profile,resume)
                if not keep:
                    metric={"professional_mismatch":"excluded_professional_mismatch","leadership_mismatch":"excluded_leadership_mismatch","level_mismatch":"excluded_level_mismatch"}[filter_reason]
                    metrics[metric]=metrics.get(metric,0)+1
                    pool_exclusions[resume_id]={"reason":filter_reason,"vacancy_level":vacancy_level,"candidate_level":candidate_level["level"],"evidence":[str(x)[:240] for x in evidence[:4]]}
                    if checkpoint:checkpoint(candidates,metrics,pool_exclusions)
                    continue
                if candidate_level["level"]=="unknown":metrics["level_unknown_kept"]=metrics.get("level_unknown_kept",0)+1
            candidates.append((resume_id, resume, notes))
            metrics["pool"] = len(candidates)
            if checkpoint:checkpoint(candidates,metrics,pool_exclusions)
            progress("history", f"Формируется пул: {len(candidates)} из {target_pool}", _progress_payload(metrics, target_pool, requested_count))
        if page + 1 >= data.get("pages", 0):
            break
    return candidates



POOL_V2_STRATEGY_ORDER = (
    "balanced",
    "title_recall",
    "functional_recall",
    "precision",
)


def _pool_v2_note(notes):
    for note in notes:
        if isinstance(note, dict) and note.get("pool_version") == 2:
            return note
    note = {
        "pool_version": 2,
        "matched_strategies": [],
        "strategy_matches": [],
        "signals": {},
        "leadership": {},
    }
    notes.append(note)
    return note


def _pool_v2_add_match(note, strategy_id, page, rank):
    if strategy_id not in note["matched_strategies"]:
        note["matched_strategies"].append(strategy_id)
        note["matched_strategies"].sort(key=POOL_V2_STRATEGY_ORDER.index)
    match = {"strategy_id": strategy_id, "page": page, "rank": rank}
    if match not in note["strategy_matches"]:
        note["strategy_matches"].append(match)


def _pool_v2_signals(resume, requirements):
    area = str((resume.get("area") or {}).get("id") or "")
    target_area = str(requirements.get("area_id") or "")
    months = (resume.get("total_experience") or {}).get("months")
    minimum = requirements.get("minimum_experience_months")
    return {
        "city": "unknown" if not area or not target_area else "match" if area == target_area else "mismatch",
        "experience": "unknown" if months is None or minimum is None else "match" if months >= minimum else "below_requested",
        "role_title": "unknown",
        "professional": "mismatch_or_unknown",
    }


def _pool_v2_strategies(plan):
    first = {}
    for strategy in plan.get("strategies") or []:
        if isinstance(strategy, dict) and strategy.get("id") not in first:
            first[strategy.get("id")] = strategy
    result = []
    for strategy_id in POOL_V2_STRATEGY_ORDER:
        strategy = first.get(strategy_id)
        if not strategy:
            continue
        normal = strategy.get("executable_now") is True and strategy.get("blocked_by") is None
        legacy = (
            strategy_id == "functional_recall"
            and strategy.get("executable_now") is False
            and strategy.get("blocked_by") == "pool_v1_requires_leading_title_group"
        )
        query = strategy.get("query")
        if (normal or legacy) and isinstance(query, str) and query.strip():
            result.append((strategy_id, query))
    return result


def _search_candidate_pool_v2(vacancy_id, vacancy, requirements, target_pool, requested_count, metrics, progress, profile, pool_exclusions, existing_pool, checkpoint, plan):
    reset_history_cache()
    candidates = copy.deepcopy(list(existing_pool or []))
    pool_exclusions = pool_exclusions if isinstance(pool_exclusions, dict) else {}
    candidate_index = {
        str(candidate[0]): index
        for index, candidate in enumerate(candidates)
        if candidate
    }
    attempted_resume_ids = set(candidate_index)
    state = metrics.setdefault("pool_v2", {})
    state.setdefault("strategy_order", list(POOL_V2_STRATEGY_ORDER))
    state.setdefault("completed_strategies", [])
    state.setdefault("current_strategy", None)
    state.setdefault("next_page_by_strategy", {})
    state.setdefault("matched_strategy_counts", {})
    completed = state["completed_strategies"]
    scan_budget = SEARCH_SCAN_LIMIT
    vacancy_level = (
        ((profile or {}).get("leadership_intent") or {}).get(
            "level",
            "unknown",
        )
    )

    for strategy_id, strategy_query in _pool_v2_strategies(plan):
        if strategy_id in completed:
            continue
        if len(candidates) >= target_pool or metrics.get("scanned", 0) >= scan_budget:
            break
        state["current_strategy"] = strategy_id
        page = int(state["next_page_by_strategy"].get(strategy_id, 0))
        state["next_page_by_strategy"].setdefault(strategy_id, page)
        strategy_completed = False
        strategy_stopped_by_limit = False

        while page < SEARCH_MAX_PAGES:
            if len(candidates) >= target_pool or metrics.get("scanned", 0) >= scan_budget:
                strategy_stopped_by_limit = True
                break
            params = {
                "text": strategy_query,
                "per_page": SEARCH_PER_PAGE,
                "page": page,
            }
            if requirements.get("area_id"):
                params["area"] = requirements["area_id"]
            data = search_resumes(params)
            items = data.get("items", [])
            pages = data.get("pages", 0)
            metrics["search_items"] = metrics.get("search_items", 0) + len(items)
            progress(
                "search",
                f"Поиск HH: {strategy_id}, страница {page + 1}",
                _progress_payload(metrics, target_pool, requested_count),
            )
            page_complete = True
            for rank, item in enumerate(items, start=1):
                if len(candidates) >= target_pool or metrics.get("scanned", 0) >= scan_budget:
                    page_complete = False
                    strategy_stopped_by_limit = True
                    break
                resume_id = str(item.get("id") or "")
                if not resume_id:
                    continue
                if resume_id in candidate_index:
                    index = candidate_index[resume_id]
                    _, _, notes = candidates[index]
                    note = _pool_v2_note(notes)
                    before = len(note["strategy_matches"])
                    _pool_v2_add_match(note, strategy_id, page, rank)
                    if len(note["strategy_matches"]) != before:
                        state["matched_strategy_counts"][strategy_id] = (
                            state["matched_strategy_counts"].get(strategy_id, 0) + 1
                        )
                        if checkpoint:
                            checkpoint(candidates, metrics, pool_exclusions)
                    continue
                if resume_id in attempted_resume_ids:
                    continue
                if was_sent_to_ai(resume_id, vacancy_id):
                    attempted_resume_ids.add(resume_id)
                    metrics["already_sent"] = metrics.get("already_sent", 0) + 1
                    continue
                metrics["scanned"] = metrics.get("scanned", 0) + 1
                attempted_resume_ids.add(resume_id)
                try:
                    loaded = fetch_resume_details(resume_id)
                except HHUnavailableError:
                    raise
                except Exception:
                    metrics["load_failed"] = metrics.get("load_failed", 0) + 1
                    logger.exception("Не удалось загрузить резюме %s", resume_id)
                    continue
                if not isinstance(loaded, dict) or not loaded:
                    metrics["load_failed"] = metrics.get("load_failed", 0) + 1
                    continue
                resume = copy.deepcopy(loaded)
                if was_sent_to_ai(resume_id, vacancy_id):
                    metrics["already_sent"] = metrics.get("already_sent", 0) + 1
                    continue
                leadership = assess_candidate_leadership_v2(resume)
                if (
                    vacancy_level == "individual_contributor"
                    and leadership["level"] == "people_manager"
                    and len(set(leadership["evidence_categories"])) >= 2
                    and leadership["confidence"] >= 0.80
                ):
                    metrics["excluded_leadership_mismatch"] = (
                        metrics.get("excluded_leadership_mismatch", 0) + 1
                    )
                    pool_exclusions[resume_id] = {
                        "reason": "leadership_mismatch",
                        "vacancy_level": "individual_contributor",
                        "candidate_level": "people_manager",
                        "confidence": leadership["confidence"],
                        "evidence_categories": list(
                            leadership["evidence_categories"]
                        ),
                        "evidence": list(leadership["evidence"]),
                    }
                    if checkpoint:
                        checkpoint(candidates, metrics, pool_exclusions)
                    continue
                notes = []
                note = _pool_v2_note(notes)
                _pool_v2_add_match(note, strategy_id, page, rank)
                note["signals"] = _pool_v2_signals(resume, requirements)
                note["leadership"] = copy.deepcopy(leadership)
                candidates.append((resume_id, resume, notes))
                candidate_index[resume_id] = len(candidates) - 1
                state["matched_strategy_counts"][strategy_id] = (
                    state["matched_strategy_counts"].get(strategy_id, 0) + 1
                )
                metrics["pool"] = len(candidates)
                if leadership["level"] == "unknown":
                    metrics["level_unknown_kept"] = (
                        metrics.get("level_unknown_kept", 0) + 1
                    )
                if checkpoint:
                    checkpoint(candidates, metrics, pool_exclusions)
                progress(
                    "history",
                    f"Формируется пул: {len(candidates)} из {target_pool}",
                    _progress_payload(metrics, target_pool, requested_count),
                )
            if not page_complete:
                break
            page += 1
            state["next_page_by_strategy"][strategy_id] = page
            if checkpoint:
                checkpoint(candidates, metrics, pool_exclusions)
            if page >= pages:
                strategy_completed = True
                break

        if strategy_completed:
            if strategy_id not in completed:
                completed.append(strategy_id)
            state["current_strategy"] = None
            if checkpoint:
                checkpoint(candidates, metrics, pool_exclusions)
        elif strategy_stopped_by_limit:
            state["current_strategy"] = strategy_id
            if checkpoint:
                checkpoint(candidates, metrics, pool_exclusions)

    metrics["pool"] = len(candidates)
    return candidates


def _select_candidate_pool_version(checkpoint):
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    explicit = checkpoint.get("pool_version")
    if explicit in (1, 2, 3) and not isinstance(explicit, bool):
        return {"pool_version": explicit}
    if not checkpoint:
        if settings.CANDIDATE_POOL_V3_ENABLED:
            return {"pool_version": 3}
        if settings.CANDIDATE_POOL_V2_ENABLED:
            return {"pool_version": 2}
        return {"pool_version": 1}

    metrics = checkpoint.get("metrics")
    has_v2_metrics = isinstance(metrics, dict) and isinstance(metrics.get("pool_v2"), dict)
    has_v2_note = any(
        isinstance(candidate, (list, tuple))
        and len(candidate) >= 3
        and any(isinstance(note, dict) and note.get("pool_version") == 2 for note in (candidate[2] or []))
        for candidate in (checkpoint.get("pool") or [])
    )
    has_complete_v2 = has_v2_metrics and has_v2_note
    has_partial_v2 = has_v2_metrics != has_v2_note
    has_v3 = "page_snapshot" in checkpoint or "scanned_new_candidates" in checkpoint
    v1_keys = {"vacancy", "pool", "pool_complete", "metrics", "pool_exclusions"}
    has_v1 = v1_keys.issubset(checkpoint)

    if has_partial_v2:
        return {
            "resolution": "manual_resolution",
            "reason": "unknown_legacy_pool_version",
        }
    if has_complete_v2 and not has_v3:
        return {"pool_version": 2}
    if has_v1 and not has_complete_v2 and not has_v3:
        return {"pool_version": 1}
    return {
        "resolution": "manual_resolution",
        "reason": "unknown_legacy_pool_version",
    }

def _search_candidate_pool_v3(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile=None, pool_exclusions=None, existing_pool=None, checkpoint=None, plan=None):
    import json
    import math
    import requests

    guard_key = "details_failure_guard"
    guard_keys = {
        "schema_version", "completed_resume_ids", "recent_outcomes",
        "consecutive_failures", "attempts_total", "failures_total",
        "not_found_failures", "malformed_failures", "generic_failures",
        "trigger",
    }
    failure_outcomes = {"not_found", "malformed_response", "generic_error"}
    allowed_outcomes = failure_outcomes | {"success"}
    window = settings.CANDIDATE_POOL_V3_FAILURE_WINDOW
    minimum_sample = settings.CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE
    rate_threshold = settings.CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD
    consecutive_limit = settings.CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT
    guard_message = (
        "Слишком много резюме не удалось загрузить. "
        "Повторите попытку позже или проверьте состояние поиска."
    )
    malformed_state_message = (
        "Некорректное состояние защиты Candidate Pool V3. "
        "Требуется безопасное восстановление состояния."
    )

    def nonnegative_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def malformed_state():
        raise UserFacingError(malformed_state_message)

    def validate_trigger(trigger):
        if trigger is None:
            return
        expected = {
            "kind", "consecutive_failures", "sample_size",
            "failure_count", "failure_rate",
        }
        if not isinstance(trigger, dict) or set(trigger) != expected:
            malformed_state()
        kind = trigger["kind"]
        consecutive = trigger["consecutive_failures"]
        sample_size = trigger["sample_size"]
        failure_count = trigger["failure_count"]
        failure_rate = trigger["failure_rate"]
        if kind not in {"consecutive", "rate"}:
            malformed_state()
        if not all(nonnegative_int(value) for value in (consecutive, sample_size, failure_count)):
            malformed_state()
        if failure_count > sample_size or sample_size > window:
            malformed_state()
        if (
            not isinstance(failure_rate, (int, float))
            or isinstance(failure_rate, bool)
            or not 0.0 <= failure_rate <= 1.0
        ):
            malformed_state()
        if sample_size:
            if not math.isclose(
                float(failure_rate),
                failure_count / sample_size,
                rel_tol=1e-9,
                abs_tol=1e-12,
            ):
                malformed_state()
        elif failure_count or float(failure_rate) != 0.0:
            malformed_state()
        if kind == "consecutive" and consecutive < consecutive_limit:
            malformed_state()
        if kind == "rate" and (
            sample_size < minimum_sample or float(failure_rate) < rate_threshold
        ):
            malformed_state()

    def validate_guard(value):
        if not isinstance(value, dict) or set(value) != guard_keys:
            malformed_state()
        if (
            not isinstance(value["schema_version"], int)
            or isinstance(value["schema_version"], bool)
            or value["schema_version"] != 1
        ):
            malformed_state()
        completed = value["completed_resume_ids"]
        if (
            not isinstance(completed, list)
            or any(not isinstance(item, str) or not item for item in completed)
            or len(completed) != len(set(completed))
        ):
            malformed_state()
        recent = value["recent_outcomes"]
        if (
            not isinstance(recent, list)
            or len(recent) > window
            or any(item not in allowed_outcomes for item in recent)
        ):
            malformed_state()
        counter_names = (
            "consecutive_failures", "attempts_total", "failures_total",
            "not_found_failures", "malformed_failures", "generic_failures",
        )
        if any(not nonnegative_int(value[name]) for name in counter_names):
            malformed_state()
        failures = value["failures_total"]
        if value["attempts_total"] < failures:
            malformed_state()
        if failures != (
            value["not_found_failures"]
            + value["malformed_failures"]
            + value["generic_failures"]
        ):
            malformed_state()
        if value["consecutive_failures"] > failures:
            malformed_state()
        trigger = value["trigger"]
        validate_trigger(trigger)
        if trigger is not None:
            actual_consecutive_failures = value["consecutive_failures"]
            actual_sample_size = len(recent)
            actual_failure_count = sum(
                outcome in failure_outcomes for outcome in recent
            )
            actual_failure_rate = (
                actual_failure_count / actual_sample_size
                if actual_sample_size
                else 0.0
            )
            if (
                trigger["consecutive_failures"] != actual_consecutive_failures
                or trigger["sample_size"] != actual_sample_size
                or trigger["failure_count"] != actual_failure_count
                or not math.isclose(
                    float(trigger["failure_rate"]),
                    actual_failure_rate,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
            ):
                malformed_state()

    def trigger_for(state):
        sample_size = len(state["recent_outcomes"])
        failure_count = sum(
            outcome in failure_outcomes for outcome in state["recent_outcomes"]
        )
        failure_rate = failure_count / sample_size if sample_size else 0.0
        if state["consecutive_failures"] >= consecutive_limit:
            kind = "consecutive"
        elif sample_size >= minimum_sample and failure_rate >= rate_threshold:
            kind = "rate"
        else:
            return None
        return {
            "kind": kind,
            "consecutive_failures": state["consecutive_failures"],
            "sample_size": sample_size,
            "failure_count": failure_count,
            "failure_rate": failure_rate,
        }

    def append_outcome(state, outcome, resume_id):
        state["recent_outcomes"].append(outcome)
        if len(state["recent_outcomes"]) > window:
            state["recent_outcomes"] = state["recent_outcomes"][-window:]
        state["attempts_total"] += 1
        if outcome == "success":
            state["consecutive_failures"] = 0
        else:
            state["failures_total"] += 1
            state["consecutive_failures"] += 1
            state[{
                "not_found": "not_found_failures",
                "malformed_response": "malformed_failures",
                "generic_error": "generic_failures",
            }[outcome]] += 1
        state["completed_resume_ids"].append(resume_id)

    reset_history_cache()
    if guard_key not in metrics:
        guard = {
            "schema_version": 1,
            "completed_resume_ids": [],
            "recent_outcomes": [],
            "consecutive_failures": 0,
            "attempts_total": 0,
            "failures_total": 0,
            "not_found_failures": 0,
            "malformed_failures": 0,
            "generic_failures": 0,
            "trigger": None,
        }
    else:
        raw_guard = metrics[guard_key]
        validate_guard(raw_guard)
        guard = copy.deepcopy(raw_guard)
    metrics[guard_key] = guard
    if guard["trigger"] is not None:
        raise UserFacingError(guard_message)

    candidates = copy.deepcopy(list(existing_pool or []))
    exclusions = copy.deepcopy(pool_exclusions) if isinstance(pool_exclusions, dict) else {}
    seen = {
        normalize(candidate[0])
        for candidate in candidates
        if candidate and normalize(candidate[0])
    }
    seen.update(normalize(resume_id) for resume_id in exclusions if normalize(resume_id))
    seen.update(
        normalize(resume_id)
        for resume_id in guard["completed_resume_ids"]
        if normalize(resume_id)
    )
    for key in (
        "search_items_seen", "unique_resume_ids_seen", "scanned_new_candidates",
        "duplicates_skipped", "blocked_by_ai_history", "invalid_resume_id",
        "load_failed", "pages_fetched",
    ):
        metrics.setdefault(key, 0)
    max_pages = settings.CANDIDATE_POOL_V3_MAX_PAGES
    scan_limit = settings.CANDIDATE_POOL_V3_SCAN_LIMIT
    stop_reason = None
    page = 0
    while page < max_pages:
        if len(candidates) >= target_pool:
            stop_reason = "target_pool_reached"; break
        if metrics["scanned_new_candidates"] >= scan_limit:
            stop_reason = "scan_limit_reached"; break
        try:
            data = search_resumes({"text": query, "per_page": SEARCH_PER_PAGE, "page": page})
        except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as exc:
            raise HHUnavailableError(
                "HH вернул некорректный ответ при поиске резюме. Повторите попытку позже.",
                error_kind="malformed_response", request_scope="search",
                endpoint="/resumes", status_code=None,
            ) from exc
        malformed = (
            not isinstance(data, dict)
            or "items" not in data or not isinstance(data.get("items"), list)
            or "pages" not in data
            or not isinstance(data.get("pages"), int)
            or isinstance(data.get("pages"), bool)
            or data.get("pages") < 0
        )
        if malformed:
            raise HHUnavailableError(
                "HH вернул некорректный ответ при поиске резюме. Повторите попытку позже.",
                error_kind="malformed_response", request_scope="search",
                endpoint="/resumes", status_code=None,
            )
        items = data["items"]
        pages = data["pages"]
        metrics["pages_fetched"] += 1
        progress("search", f"Поиск HH: страница {page + 1}", _progress_payload(metrics, target_pool, requested_count))
        for item in items:
            if len(candidates) >= target_pool:
                stop_reason = "target_pool_reached"; break
            if metrics["scanned_new_candidates"] >= scan_limit:
                stop_reason = "scan_limit_reached"; break
            metrics["search_items_seen"] += 1
            resume_id = normalize(item.get("id") if isinstance(item, dict) else None)
            if not resume_id:
                metrics["invalid_resume_id"] += 1; continue
            if resume_id in seen:
                metrics["duplicates_skipped"] += 1; continue
            seen.add(resume_id)
            metrics["unique_resume_ids_seen"] += 1
            if was_sent_to_ai(resume_id, vacancy_id):
                metrics["blocked_by_ai_history"] += 1; continue
            metrics["scanned_new_candidates"] += 1
            try:
                loaded = fetch_resume_details(resume_id)
            except HHUnavailableError as exc:
                if (
                    getattr(exc, "request_scope", None) == "details"
                    and getattr(exc, "error_kind", None) in {"not_found", "malformed_response"}
                ):
                    outcome = getattr(exc, "error_kind")
                    append_outcome(guard, outcome, resume_id)
                    metrics["load_failed"] += 1
                    guard["trigger"] = trigger_for(guard)
                    logger.warning("Не удалось загрузить резюме %s: структурированная ошибка HH", resume_id)
                    if checkpoint: checkpoint(candidates, metrics, exclusions)
                    if guard["trigger"] is not None:
                        raise UserFacingError(guard_message)
                    continue
                raise
            except Exception:
                append_outcome(guard, "generic_error", resume_id)
                metrics["load_failed"] += 1
                guard["trigger"] = trigger_for(guard)
                logger.exception("Не удалось загрузить резюме %s", resume_id)
                if checkpoint: checkpoint(candidates, metrics, exclusions)
                if guard["trigger"] is not None:
                    raise UserFacingError(guard_message)
                continue
            resume = copy.deepcopy(loaded)
            append_outcome(guard, "success", resume_id)
            candidates.append((resume_id, resume, []))
            if len(candidates) >= target_pool:
                guard["trigger"] = None
                metrics["stop_reason"] = "target_pool_reached"
                if checkpoint: checkpoint(candidates, metrics, exclusions)
                return {
                    "pool": candidates, "pool_complete": True,
                    "stop_reason": "target_pool_reached", "metrics": metrics,
                    "pool_exclusions": exclusions,
                }
            guard["trigger"] = trigger_for(guard)
            if checkpoint: checkpoint(candidates, metrics, exclusions)
            if guard["trigger"] is not None:
                raise UserFacingError(guard_message)
        if stop_reason is None:
            if len(candidates) >= target_pool: stop_reason = "target_pool_reached"
            elif metrics["scanned_new_candidates"] >= scan_limit: stop_reason = "scan_limit_reached"
        if stop_reason is not None: break
        if checkpoint: checkpoint(candidates, metrics, exclusions)
        page += 1
        if page >= pages:
            stop_reason = "search_exhausted"; break
    if stop_reason is None:
        if len(candidates) >= target_pool: stop_reason = "target_pool_reached"
        elif metrics["scanned_new_candidates"] >= scan_limit: stop_reason = "scan_limit_reached"
        else: stop_reason = "page_limit_reached"
    metrics["stop_reason"] = stop_reason
    if checkpoint: checkpoint(candidates, metrics, exclusions)
    return {"pool": candidates, "pool_complete": True, "stop_reason": stop_reason, "metrics": metrics, "pool_exclusions": exclusions}

def _search_candidate_pool(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile=None, pool_exclusions=None, existing_pool=None, checkpoint=None, plan=None):
    if settings.CANDIDATE_POOL_V3_ENABLED:
        raise UserFacingError(
            "Candidate Pool V3 выбран, но production execution routing ещё не разрешён."
        )
    if settings.CANDIDATE_POOL_V2_ENABLED and isinstance(plan, dict) and isinstance(plan.get("strategies"), list):
        return _search_candidate_pool_v2(vacancy_id, vacancy, requirements, target_pool, requested_count, metrics, progress, profile, pool_exclusions, existing_pool, checkpoint, plan)
    return _search_candidate_pool_v1(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile, pool_exclusions, existing_pool, checkpoint)

def find_and_evaluate(vacancy_id, requested_count, responsible_email, task_id, progress=lambda *_: None, approved_query=None):
    """Compatibility entry point. All HH processing uses workflow.run_hh."""
    from app.persistence.app_state import get_task,update_task
    from app.services.workflow import run_hh
    task=get_task(task_id)
    if not task or task['mode']!='hh':raise ValueError('Требуется сохранённая HH-задача')
    if str(task['vacancy_id'])!=str(vacancy_id) or task['requested_count']!=int(requested_count) or task['responsible_email']!=responsible_email.strip().casefold():
        raise ValueError('Параметры не соответствуют сохранённой задаче')
    if approved_query is not None:update_task(task_id,query=approved_query)
    return run_hh(task_id,progress)


def prepare_query(vacancy_id, task_id, mode):
    missing = validate_nano_settings()
    if missing: raise AIUnavailableError("Не заполнены настройки AI: " + ", ".join(missing), request_sent=False)
    vacancy = fetch_vacancy_details(vacancy_id)
    if mode == 'xray':
        from app.core.errors import UserFacingError
        from app.persistence.app_state import get_task
        from app.integrations.xray.xray_plan import build_plan, DEFAULT_SOURCES, city_name
        task=get_task(task_id);city=city_name(vacancy)
        try: query=build_plan(generate_xray_query(vacancy),task.get('xray_sources',DEFAULT_SOURCES),city)
        except ValueError as exc: raise UserFacingError(str(exc)) from exc
        set_review(task_id,query,vacancy_search_profile=None)
    else:
        result=generate_vacancy_search_plan(vacancy);query=result["query"]
        set_review(task_id,query,vacancy_search_profile=result["profile"],vacancy_search_plan=result["plan"])


def finish_xray_query(task_id, query):
    # Deliberately no HH fallback and no resume evaluation in X-Ray mode.
    from urllib.parse import urlencode
    return {"mode": "xray", "query": query,
            "search_url": "https://www.google.com/search?" + urlencode({"q": query}),
            "message": "X-Ray запрос подтверждён. Внешний поисковый API не подключён; откройте запрос в поисковике. Резюме не отправлялись в ИИ.",
            "evaluated": [], "manual_review": [], "metrics": {"ai_requested": 0}}
