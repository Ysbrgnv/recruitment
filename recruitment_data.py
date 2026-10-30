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

