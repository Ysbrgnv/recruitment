import logging
import re
import time
import copy

from app.persistence.app_state import set_review, get_task, update_task
from app.core.errors import AIUnavailableError, HHUnavailableError
from app.core import settings
from app.integrations.hh.hh_client import fetch_resume_details, fetch_vacancy_details, search_resumes
from app.integrations.ai.nano_client import evaluate_one_resume, evaluation_to_text, generate_vacancy_search_plan, generate_xray_query
from app.services.recruitment_data import (assess_candidate_leadership_v2, classify_candidate_level, resume_filter_result, select_current_or_latest_experience, vacancy_requirements)
from app.core.settings import (
    MAX_CANDIDATE_POOL, POOL_EXTRA, POOL_MULTIPLIER,
    SEARCH_MAX_PAGES, SEARCH_PER_PAGE, SEARCH_SCAN_LIMIT,
    validate_nano_settings,
)
from app.persistence.tracking_service import reset_history_cache, was_sent_to_ai

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


def _search_candidate_pool(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile=None, pool_exclusions=None, existing_pool=None, checkpoint=None, plan=None):
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
