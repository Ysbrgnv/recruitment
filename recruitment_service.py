import logging
import re
import time

from app_state import set_review, get_task, update_task
from errors import AIUnavailableError, HHUnavailableError
from hh_client import fetch_resume_details, fetch_vacancy_details, search_resumes
from nano_client import evaluate_one_resume, evaluation_to_text, generate_resume_language_query, generate_xray_query
from recruitment_data import resume_filter_result, vacancy_requirements
from settings import (
    MAX_CANDIDATE_POOL, POOL_EXTRA, POOL_MULTIPLIER,
    SEARCH_MAX_PAGES, SEARCH_PER_PAGE, SEARCH_SCAN_LIMIT,
    validate_nano_settings,
)
from tracking_service import reset_history_cache, was_sent_to_ai

logger = logging.getLogger(__name__)

ROLE_STOP_WORDS = {"senior", "middle", "junior", "специалист", "менеджер", "manager", "инженер", "engineer", "developer", "разработчик"}
PRESALES_MARKERS = ("pre sale", "pre sales", "presale", "presales", "пресейл", "пре сейл")
TELEMARKETING_CONTRACT_MARKERS = (
    "телемаркетинг", "телемаркетинга", "телемаркетингу", "телемаркетолог",
    "оператор исходящих звонков", "оператор исходящего обзвона",
    "специалист телефонных продаж", "менеджер телефонных продаж",
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

def _current_or_latest_positions(resume):
    experience = [item for item in resume.get("experience") or [] if isinstance(item, dict)]
    current = [item for item in experience if item.get("end") in (None, "")]
    selected = current or ([max(experience, key=lambda item: _normalized_start(item.get("start")))] if experience else [])
    return [item.get("position") or item.get("name") or item.get("title") or "" for item in selected]

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
    return bool(variants) and any(_role_text_matches(text, variants) for text in _role_texts(resume))



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


def _search_candidate_pool(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress):
    reset_history_cache()
    candidates, seen = [], set()
    for page in range(SEARCH_MAX_PAGES):
        if metrics["scanned"] >= SEARCH_SCAN_LIMIT or len(candidates) >= target_pool:
            break
        params = {"text": query, "per_page": SEARCH_PER_PAGE, "page": page}
        if requirements.get("area_id"):
            params["area"] = requirements["area_id"]
        data = search_resumes(params)
        items = data.get("items", [])
        metrics["search_items"] += len(items)
        progress("search", f"Поиск HH: страница {page + 1}", _progress_payload(metrics, target_pool, requested_count))
        for item in items:
            if metrics["scanned"] >= SEARCH_SCAN_LIMIT or len(candidates) >= target_pool:
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
                if reason == "leadership":
                    metrics["excluded_leadership"] = metrics.get("excluded_leadership", 0) + 1
                elif reason == "city":
                    metrics["excluded_city"] += 1
                else:
                    metrics["excluded_experience"] += 1
                continue
            if not _resume_role_matches_query(resume, query):
                metrics["excluded_role_mismatch"] = metrics.get("excluded_role_mismatch", 0) + 1
                continue
            if was_sent_to_ai(resume_id, vacancy_id):
                metrics["already_sent"] += 1
                continue
            candidates.append((resume_id, resume, notes))
            metrics["pool"] = len(candidates)
            progress("history", f"Формируется пул: {len(candidates)} из {target_pool}", _progress_payload(metrics, target_pool, requested_count))
        if page + 1 >= data.get("pages", 0):
            break
    return candidates


def find_and_evaluate(vacancy_id, requested_count, responsible_email, task_id, progress=lambda *_: None, approved_query=None):
    """Compatibility entry point. All HH processing uses workflow.run_hh."""
    from app_state import get_task,update_task
    from workflow import run_hh
    task=get_task(task_id)
    if not task or task['mode']!='hh':raise ValueError('Требуется сохранённая HH-задача')
    if str(task['vacancy_id'])!=str(vacancy_id) or task['requested_count']!=int(requested_count) or task['responsible_email']!=responsible_email.strip().casefold():
        raise ValueError('Параметры не соответствуют сохранённой задаче')
    if approved_query is not None:update_task(task_id,query=approved_query)
    return run_hh(task_id,progress)


def prepare_query(vacancy_id, task_id, mode):
    missing = validate_nano_settings()
    if missing:
        raise AIUnavailableError("Не заполнены настройки AI: " + ", ".join(missing), request_sent=False)
    vacancy = fetch_vacancy_details(vacancy_id)
    if mode == 'xray':
        from errors import UserFacingError
        from app_state import get_task
        from xray_plan import build_plan, DEFAULT_SOURCES, city_name
        task=get_task(task_id)
        city=city_name(vacancy)
        try:query=build_plan(generate_xray_query(vacancy),task.get('xray_sources',DEFAULT_SOURCES),city)
        except ValueError as exc:raise UserFacingError(str(exc)) from exc
    else:
        query=generate_resume_language_query(vacancy)
    set_review(task_id, query)


def finish_xray_query(task_id, query):
    # Deliberately no HH fallback and no resume evaluation in X-Ray mode.
    from urllib.parse import urlencode
    return {"mode": "xray", "query": query,
            "search_url": "https://www.google.com/search?" + urlencode({"q": query}),
            "message": "X-Ray запрос подтверждён. Внешний поисковый API не подключён; откройте запрос в поисковике. Резюме не отправлялись в ИИ.",
            "evaluated": [], "manual_review": [], "metrics": {"ai_requested": 0}}

