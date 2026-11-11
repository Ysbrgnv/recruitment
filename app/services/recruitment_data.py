import html
import re
from datetime import date, datetime

_STRONG_LEADERSHIP_PATTERNS = (
    r"(?<!\w)head\s+of\s+[\w ]+(?!\w)",
    r"(?<!\w)department\s+head(?!\w)",
    r"(?<!\w)director\s+of\s+[\w ]+(?!\w)",
    r"(?<!\w)chief\s+[\w ]+\s+officer(?!\w)",
    r"(?<!\w)руководитель\s+(?:отдела|управления|департамента|направления)(?!\w)",
    r"(?<!\w)начальник\s+(?:отдела|управления|департамента)(?!\w)",
    r"(?<!\w)директор\s+(?:отдела|управления|департамента|направления|по\s+[\w ]+)(?!\w)",
)
_MANAGEMENT_EVIDENCE_PATTERNS = (
    r"(?<!\w)управлен(?:ие|ия)\s+команд(?:ой|ы)(?:\s+из\s+\d+\s+человек)?(?!\w)",
    r"(?<!\w)руководство\s+(?:командой|подразделением|отделом|управлением)(?!\w)",
    r"(?<!\w)people\s+management(?:\s+and\s+hiring)?(?!\w)",
    r"(?<!\w)\d+\s+direct\s+reports?(?!\w)",
    r"(?<!\w)performance\s+management\s+responsibilit(?:y|ies)(?!\w)",
)
_AMBIGUOUS_LEVEL_PATTERNS = (
    r"(?<!\w)lead\s+engineer(?!\w)",
    r"(?<!\w)ведущий\s+инженер(?!\w)",
    r"(?<!\w)team\s+manager(?!\w)",
)
EXPERIENCE_MIN_MONTHS = {
    "noExperience": 0,
    "between1And3": 12,
    "between3And6": 36,
    "moreThan6": 72,
}

LEADERSHIP_MARKERS = (
    "руководитель",
    "директор",
    "начальник",
    "глава отдела",
    "глава направления",
    "заведующий",
    "head of",
    "head",
    "director",
    "chief",
    "ceo",
    "cfo",
    "coo",
    "cto",
    "team lead",
    "department head",
)

LEADERSHIP_DESCRIPTION_MARKERS = (
    "руководство командой",
    "управление подразделением",
    "управление отделом",
    "direct reports",
    "people management",
)

LEADERSHIP_NOTE = "Руководящий профиль не соответствует неруководящей вакансии"


def clean_html(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _normalized_text(value):
    value = str(value or "").casefold().replace("ё", "е")
    value = re.sub(r"[‐‑‒–—−_/\\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _contains_phrase(text, phrase):
    words = re.findall(r"\w+", _normalized_text(phrase), flags=re.UNICODE)
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)"
    return re.search(pattern, _normalized_text(text), flags=re.UNICODE) is not None


def _has_leadership_marker(value, markers=LEADERSHIP_MARKERS):
    return any(_contains_phrase(value, marker) for marker in markers)


def _is_leadership_vacancy(vacancy):
    if _has_leadership_marker(vacancy.get("name")):
        return True
    description = clean_html(vacancy.get("description"))
    return _has_leadership_marker(description, LEADERSHIP_DESCRIPTION_MARKERS)


def vacancy_requirements(vacancy):
    return {
        "area_id": str((vacancy.get("area") or {}).get("id") or ""),
        "area_name": str((vacancy.get("area") or {}).get("name") or "").casefold(),
        "minimum_experience_months": EXPERIENCE_MIN_MONTHS.get(
            (vacancy.get("experience") or {}).get("id")
        ),
        "is_leadership_vacancy": _is_leadership_vacancy(vacancy),
    }


def _professional_role_names(resume):
    names = []
    for role in resume.get("professional_roles") or []:
        if isinstance(role, dict):
            name = role.get("name")
        elif isinstance(role, str):
            name = role
        else:
            name = None
        if name:
            names.append(name)
    return names


def _experience_position(item):
    return item.get("position") or item.get("name") or item.get("title") or ""


def _normalized_start(value):
    numbers = [int(number) for number in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((numbers + [0, 0, 0])[:3])


def _relevant_experience_positions(resume):
    experience = [item for item in resume.get("experience") or [] if isinstance(item, dict)]
    current = [item for item in experience if item.get("end") in (None, "")]
    selected = current
    if not selected and experience:
        selected = [max(experience, key=lambda item: _normalized_start(item.get("start")))]
    return [_experience_position(item) for item in selected if _experience_position(item)]


def _is_leadership_resume(resume):
    if _has_leadership_marker(resume.get("title")):
        return True
    if any(_has_leadership_marker(name) for name in _professional_role_names(resume)):
        return True
    return any(_has_leadership_marker(position) for position in _relevant_experience_positions(resume))


def resume_filter_result(resume, requirements):
    area = resume.get("area") or {}
    resume_area_id = str(area.get("id") or "")
    resume_area_name = str(area.get("name") or "").casefold()
    target_area_id = requirements.get("area_id") or ""
    target_area_name = requirements.get("area_name") or ""
    if (target_area_id or target_area_name) and not (
        (target_area_id and resume_area_id == target_area_id)
        or (target_area_name and resume_area_name == target_area_name)
    ):
        return False, ["Другой город"], "city"
    if not requirements.get("is_leadership_vacancy", False) and _is_leadership_resume(resume):
        return False, [LEADERSHIP_NOTE], "leadership"
    months = (resume.get("total_experience") or {}).get("months")
    minimum = requirements.get("minimum_experience_months")
    notes = []
    if months is None:
        notes.append("Общий опыт не указан. Требуется ручная проверка.")
    elif minimum is not None and months < minimum:
        return False, ["Недостаточный опыт"], "experience"
    return True, notes, "accepted"


def prepare_vacancy(vacancy):
    return {
        "name": vacancy.get("name"),
        "city": (vacancy.get("area") or {}).get("name"),
        "experience": vacancy.get("experience"),
        "employment": vacancy.get("employment"),
        "schedule": vacancy.get("schedule"),
        "salary": vacancy.get("salary"),
        "skills": [item.get("name") for item in vacancy.get("key_skills") or []],
        "description": clean_html(vacancy.get("description")),
    }


def prepare_full_resume(resume, notes):
    return {
        "title": resume.get("title"),
        "city": (resume.get("area") or {}).get("name"),
        "total_experience": resume.get("total_experience"),
        "salary": resume.get("salary"),
        "skills": resume.get("skill_set"),
        "professional_roles": resume.get("professional_roles"),
        "experience": resume.get("experience"),
        "education": resume.get("education"),
        "additional_education": resume.get("additional_education"),
        "attestations": resume.get("attestations"),
        "languages": resume.get("language"),
        "certificates": resume.get("certificate"),
        "employment": resume.get("employments"),
        "schedules": resume.get("schedules"),
        "work_format": resume.get("work_format"),
        "business_trip_readiness": resume.get("business_trip_readiness"),
        "relocation": resume.get("relocation"),
        "filter_notes": notes,
    }

def _experience_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, pattern)
            if pattern == "%Y-%m":
                return date(parsed.year, parsed.month, 1)
            if pattern == "%Y":
                return date(parsed.year, 1, 1)
            return parsed.date()
        except ValueError:
            pass
    return None


def _experience_title(item):
    if not isinstance(item, dict):
        return None
    value = item.get("position") or item.get("title")
    value = clean_html(value) if isinstance(value, str) else ""
    return value or None


def _experience_company(item):
    value = item.get("company") if isinstance(item, dict) else None
    if isinstance(value, dict):
        value = value.get("name")
    value = clean_html(value) if isinstance(value, str) else ""
    return value or None


def _current_role_result(status="unknown", item=None, evidence=None):
    item = item if isinstance(item, dict) else None
    return {
        "status": status,
        "item": item,
        "title": _experience_title(item),
        "responsibilities": clean_html((item or {}).get("description")),
        "company": _experience_company(item),
        "evidence": list(evidence or []),
    }


def select_current_or_latest_experience(resume):
    experience = resume.get("experience") if isinstance(resume, dict) else None
    if not isinstance(experience, list) or not experience:
        return _current_role_result(evidence=["Опыт работы отсутствует или неструктурирован."])
    items = [item for item in experience if isinstance(item, dict) and _experience_title(item)]
    ongoing = []
    for item in items:
        if "end" in item and item.get("end") in (None, "") and _experience_date(item.get("start")):
            ongoing.append(item)
    if ongoing:
        titles = {(_experience_title(item) or "").casefold() for item in ongoing}
        if len(ongoing) > 1 and len(titles) > 1:
            return _current_role_result(evidence=["Найдено несколько противоречивых текущих должностей."])
        selected = max(ongoing, key=lambda item: _experience_date(item.get("start")) or date.min)
        return _current_role_result("current", selected, [f"Текущая должность: {_experience_title(selected)}"])
    completed = []
    for item in items:
        end = _experience_date(item.get("end"))
        if end:
            completed.append((end, _experience_date(item.get("start")), item))
    if not completed:
        return _current_role_result(evidence=["Не удалось определить актуальную должность по датам опыта."])
    latest_end = max(entry[0] for entry in completed)
    end_ties = [entry for entry in completed if entry[0] == latest_end]
    valid_starts = [entry[1] for entry in end_ties if entry[1] is not None]
    if len(end_ties) > 1:
        if not valid_starts:
            return _current_role_result(evidence=["Несколько должностей имеют одинаковую последнюю дату окончания."])
        latest_start = max(valid_starts)
        final = [entry for entry in end_ties if entry[1] == latest_start]
        if len(final) != 1:
            return _current_role_result(evidence=["Последняя должность неоднозначна после сравнения дат."])
        selected = final[0][2]
    else:
        selected = end_ties[0][2]
    return _current_role_result("latest", selected, [f"Последняя должность: {_experience_title(selected)}"])


def classify_candidate_level(current_role):
    if not isinstance(current_role, dict) or current_role.get("status") not in {"current", "latest"}:
        return {"level": "unknown", "evidence": ["Текущая или последняя должность не определена."]}
    title = clean_html(current_role.get("title"))
    responsibilities = clean_html(current_role.get("responsibilities"))
    title_fold = title.casefold()
    responsibilities_fold = responsibilities.casefold()
    for pattern in _STRONG_LEADERSHIP_PATTERNS:
        if re.search(pattern, title_fold, re.I):
            return {"level": "leadership", "evidence": [f"Руководящий title: {title}"]}
    for pattern in _MANAGEMENT_EVIDENCE_PATTERNS:
        match = re.search(pattern, responsibilities_fold, re.I)
        if match:
            return {"level": "leadership", "evidence": [f"Управленческая обязанность: {match.group(0)}"]}
    if not title:
        return {"level": "unknown", "evidence": ["Название текущей или последней должности отсутствует."]}
    if any(re.search(pattern, title_fold, re.I) for pattern in _AMBIGUOUS_LEVEL_PATTERNS):
        return {"level": "unknown", "evidence": [f"Неоднозначный уровень роли: {title}"]}
    return {"level": "individual_contributor", "evidence": [f"Текущая или последняя роль без подтвержденного управления: {title}"]}
