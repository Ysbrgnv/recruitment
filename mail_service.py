import html

from errors import MailUnavailableError
from gmail_transport import send_html

LABELS = {
    "mandatory_requirements": "Обязательные требования",
    "relevant_experience": "Релевантный опыт",
    "responsibilities": "Роль и обязанности",
    "education_and_certificates": "Образование и сертификаты",
    "desirable_requirements": "Желательные требования",
    "additional_conditions": "Дополнительные условия",
}
LIMITS = {
    "mandatory_requirements": 40, "relevant_experience": 25,
    "responsibilities": 15, "education_and_certificates": 10,
    "desirable_requirements": 5, "additional_conditions": 5,
}


def e(value):
    return html.escape(str(value or ""), quote=True)


def _list_html(items, empty="Не указано"):
    values = [str(item).strip() for item in items or [] if str(item).strip()]
    if not values:
        return f"<div style='color:#777'>{e(empty)}</div>"
    return "<ul style='margin:8px 0 0;padding-left:20px'>" + "".join(
        f"<li style='margin:0 0 7px'>{e(item)}</li>" for item in values
    ) + "</ul>"


def _score_rows(evaluation):
    if not evaluation.get("structured"):
        return ""
    rows = []
    for key, value in evaluation["scores"].items():
        evidence = _list_html(evaluation.get("evidence", {}).get(key), "Подтверждение не указано")
        rows.append(
            "<tr>"
            f"<td style='padding:11px 14px;border-bottom:1px solid #ececec;vertical-align:top'><b>{e(LABELS[key])}</b>{evidence}</td>"
            f"<td width='90' align='center' style='padding:11px 12px;border-bottom:1px solid #ececec;vertical-align:top'>"
            f"<span style='font-size:16px;font-weight:800'>{value}/{LIMITS[key]}</span></td>"
            "</tr>"
        )
    return "".join(rows)


def build_candidate_email(vacancy: dict, item: dict, search_query: str) -> str:
    evaluation = item["evaluation"]
    structured = evaluation.get("structured")
    score = f"{evaluation.get('total')}/100" if structured else "Ручная проверка"
    level = evaluation.get("level") if structured else "Ответ ИИ получен в неструктурированном формате"
    city = (vacancy.get("area") or {}).get("name") or "Не указан"
    score_table = (
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border:1px solid #e4e4e4;border-radius:10px'>{_score_rows(evaluation)}</table>"
        if structured else
        f"<div style='padding:16px;background:#fff7cc;border-left:5px solid #FFD400'>{e(item['evaluation_text']).replace(chr(10), '<br>')}</div>"
    )
    return f"""
<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin:0;background:#f2f2f2;font-family:Segoe UI,Arial,sans-serif;color:#171717'>
<tr><td align='center' style='padding:28px 12px'>
<table role='presentation' width='720' cellpadding='0' cellspacing='0' style='width:720px;max-width:100%;background:#ffffff;border-radius:16px;overflow:hidden'>
<tr><td style='background:#111111;padding:26px 30px'>
  <div style='color:#FFD400;font-size:27px;font-weight:900;letter-spacing:.2px'>Recruitment Assistant</div>
  <div style='color:#dddddd;margin-top:6px'>Предварительное профессиональное сопоставление</div>
</td></tr>
<tr><td style='padding:28px 30px'>
  <div style='font-size:22px;font-weight:800'>{e(vacancy.get('name'))}</div>
  <div style='color:#666;margin-top:5px'>{e(city)} · Кандидат №{item['number']}</div>
  <table role='presentation' width='100%' style='margin:20px 0;background:#111;border-radius:10px'>
    <tr><td style='padding:17px 20px;color:#fff'><div style='color:#FFD400;font-weight:800'>{e(level)}</div><div style='font-size:12px;color:#bbb;margin-top:5px'>Требуется проверка рекрутером</div></td>
    <td align='right' style='padding:17px 20px'><span style='background:#FFD400;color:#111;padding:9px 13px;border-radius:8px;font-size:19px;font-weight:900'>{e(score)}</span></td></tr>
  </table>
  {score_table}
  <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin-top:20px'>
    <tr><td width='50%' style='padding:16px;background:#f7f7f7;vertical-align:top'><b>Подтверждённые сильные стороны</b>{_list_html(evaluation.get('strengths'))}</td>
    <td width='12'></td><td width='50%' style='padding:16px;background:#fff7cc;vertical-align:top'><b>Неподтверждённые требования и риски</b>{_list_html(evaluation.get('gaps'))}</td></tr>
  </table>
  <div style='margin-top:14px;padding:16px;border:1px solid #e5e5e5;border-radius:10px'><b>Недостающие профессиональные данные</b>{_list_html(evaluation.get('missing_data'), 'Дополнительные данные не отмечены')}</div>
  <div style='margin-top:14px;padding:16px;border-left:5px solid #FFD400;background:#fafafa'><b>Рекомендация для следующего шага</b><div style='margin-top:7px;line-height:1.5'>{e(evaluation.get('recommendation') or 'Требуется ручная проверка рекрутером.')}</div></div>
  <div style='margin-top:18px'><a href='{e(item['url'])}' style='display:inline-block;background:#FFD400;color:#111;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:800'>Открыть резюме в HH</a></div>
  <div style='margin-top:20px;color:#777;font-size:12px;line-height:1.45'>Поисковый запрос HH: {e(search_query)}<br>Оценка носит рекомендательный характер. Система не принимает решение о найме или отказе.</div>
</td></tr>
<tr><td style='background:#111;color:#aaa;padding:15px;text-align:center;font-size:12px'>Recruitment Assistant · Beeline style</td></tr>
</table></td></tr></table>"""


def send_candidate_email(responsible_email: str, vacancy: dict, item: dict, search_query: str) -> str:
    return send_html(responsible_email,
        f"Предварительная оценка кандидата №{item['number']}: {vacancy.get('name', 'Вакансия')}",
        build_candidate_email(vacancy,item,search_query))


def build_xray_email(vacancy, profiles, requested_count, query, delivery_id):
    from xray_client import canonical_profile
    cards=[]
    for number,profile in enumerate(profiles,1):
        url=canonical_profile(profile.get('url'))
        if not url:raise MailUnavailableError('Некорректная ссылка профиля.')
        cards.append(f"<tr><td style='padding:18px 24px;border-bottom:1px solid #e5e5e5'><b style='font-size:17px'>Профиль №{number}</b><p style='color:#666;word-break:break-all'>{e(url)}</p><a href='{e(url)}' style='display:inline-block;background:#FFD400;color:#111;padding:12px 18px;text-decoration:none;font-weight:bold;border-radius:8px'>Открыть профиль</a></td></tr>")
    found=len(profiles)
    note=f'Запрошено: {requested_count}. Найдено новых профилей: {found}.'
    if found<requested_count:note+=' Недобор: '+str(requested_count-found)+'. Других новых ссылок в доступной выдаче нет.'
    return f"""<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f2f2f2;font-family:Segoe UI,Arial,sans-serif'><tr><td align='center' style='padding:28px 12px'><table role='presentation' width='680' cellpadding='0' cellspacing='0' style='max-width:680px;background:white'><tr><td style='background:#111;color:#FFD400;padding:26px;font-size:26px;font-weight:bold'>Recruitment Assistant · X-Ray</td></tr><tr><td style='padding:24px'><h2>{e(vacancy.get('name'))}</h2><p>{e(note)}</p><p style='color:#666'>Ссылки из внешней поисковой выдачи. Резюме не оценивались ИИ. Соответствие вакансии проверяет рекрутер.</p></td></tr>{''.join(cards)}<tr><td style='padding:24px;background:#fff7cc'><b>Подтверждённый запрос</b><p style='word-break:break-word'>{e(query)}</p></td></tr><tr><td style='background:#111;color:#aaa;padding:18px;font-size:12px'>Идентификатор письма: {e(delivery_id)}</td></tr></table></td></tr></table>"""

def send_xray_email(responsible_email,vacancy,profiles,requested_count,query,delivery_id):
    return send_html(responsible_email,
        f"X-Ray: {vacancy.get('name') or ''} · {len(profiles)} профилей [{delivery_id[:12]}]",
        build_xray_email(vacancy,profiles,requested_count,query,delivery_id))

