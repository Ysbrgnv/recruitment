import logging
import time

from app_state import set_review, get_task, update_task
from errors import AIUnavailableError, HHUnavailableError
from hh_client import fetch_resume_details, fetch_vacancy_details, search_resumes
from nano_client import evaluate_one_resume, evaluation_to_text, generate_vacancy_search_plan, generate_xray_query
from recruitment_data import (classify_candidate_level, resume_filter_result, select_current_or_latest_experience, vacancy_requirements)
from settings import (
    MAX_CANDIDATE_POOL, POOL_EXTRA, POOL_MULTIPLIER,
    SEARCH_MAX_PAGES, SEARCH_PER_PAGE, SEARCH_SCAN_LIMIT,
    validate_nano_settings,
)
from tracking_service import reset_history_cache, was_sent_to_ai

logger = logging.getLogger(__name__)


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


def _search_candidate_pool(vacancy_id, vacancy, requirements, query, target_pool, requested_count, metrics, progress, profile=None, pool_exclusions=None, existing_pool=None, checkpoint=None):
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
                metrics["excluded_city" if reason == "city" else "excluded_experience"] += 1
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
        plan=generate_vacancy_search_plan(vacancy);query=plan["query"]
    set_review(task_id, query, vacancy_search_profile=plan.get("profile") if mode != 'xray' else None)


def finish_xray_query(task_id, query):
    # Deliberately no HH fallback and no resume evaluation in X-Ray mode.
    from urllib.parse import urlencode
    return {"mode": "xray", "query": query,
            "search_url": "https://www.google.com/search?" + urlencode({"q": query}),
            "message": "X-Ray запрос подтверждён. Внешний поисковый API не подключён; откройте запрос в поисковике. Резюме не отправлялись в ИИ.",
            "evaluated": [], "manual_review": [], "metrics": {"ai_requested": 0}}

