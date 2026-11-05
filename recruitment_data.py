import html, re
EXPERIENCE_MIN_MONTHS = {"noExperience":0, "between1And3":12, "between3And6":36, "moreThan6":72}
def clean_html(value): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()
def vacancy_requirements(v): return {"area_id":str((v.get("area") or {}).get("id") or ""), "area_name":str((v.get("area") or {}).get("name") or "").casefold(), "minimum_experience_months":EXPERIENCE_MIN_MONTHS.get((v.get("experience") or {}).get("id"))}
def resume_filter_result(r, req):
    area = r.get("area") or {}; rid = str(area.get("id") or ""); rname = str(area.get("name") or "").casefold(); tid = req.get("area_id") or ""; tname = req.get("area_name") or ""
    if (tid or tname) and not ((tid and rid == tid) or (tname and rname == tname)): return False, ["Другой город"], "city"
    months = (r.get("total_experience") or {}).get("months"); minimum = req.get("minimum_experience_months"); notes = []
    if months is None: notes.append("Общий опыт не указан. Требуется ручная проверка.")
    elif minimum is not None and months < minimum: return False, ["Недостаточный опыт"], "experience"
    return True, notes, "accepted"
def prepare_vacancy(v): return {"name":v.get("name"), "city":(v.get("area") or {}).get("name"), "experience":v.get("experience"), "employment":v.get("employment"), "schedule":v.get("schedule"), "salary":v.get("salary"), "skills":[x.get("name") for x in v.get("key_skills") or []], "description":clean_html(v.get("description"))}
def prepare_full_resume(r, notes): return {"title":r.get("title"), "city":(r.get("area") or {}).get("name"), "total_experience":r.get("total_experience"), "salary":r.get("salary"), "skills":r.get("skill_set"), "professional_roles":r.get("professional_roles"), "experience":r.get("experience"), "education":r.get("education"), "additional_education":r.get("additional_education"), "attestations":r.get("attestations"), "languages":r.get("language"), "certificates":r.get("certificate"), "employment":r.get("employments"), "schedules":r.get("schedules"), "work_format":r.get("work_format"), "business_trip_readiness":r.get("business_trip_readiness"), "relocation":r.get("relocation"), "filter_notes":notes}


from datetime import date, datetime


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


_STRONG_LEADERSHIP_PATTERNS = (
    r"\bhead\s+of\b", r"\bdirector\b", r"\bchief\b", r"\bdepartment\s+head\b",
    r"\bруководитель\s+(?:отдела|департамента|направления)\b",
    r"\bначальник\s+(?:отдела|управления)\b",
    r"\bдиректор\s+(?:департамента|направления)\b",
)
_MANAGEMENT_EVIDENCE_PATTERNS = (
    r"управлен(?:ие|ия|ию)\s+команд", r"руководств(?:о|а|у)\s+подразделен",
    r"\bpeople\s+management\b", r"\bdirect\s+reports?\b",
    r"\bhiring\b", r"performance\s+(?:management|review|responsibility)",
)
_AMBIGUOUS_LEVEL_PATTERNS = (
    r"\blead\b", r"\bteam\s+manager\b", r"\bведущ(?:ий|ая|ее|ие)\b",
)


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
