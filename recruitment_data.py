import html
import re

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
