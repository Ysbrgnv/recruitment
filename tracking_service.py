"""SQLite history with atomic reservations; no Excel/COM in the processing path."""
from __future__ import annotations
import json
import time
from datetime import datetime
from app_state import database
from app.core.errors import TrackerUnavailableError
from app.core.settings import RESERVATION_STALE_MINUTES

def normalize(value):
    if isinstance(value,float) and value.is_integer():return str(int(value))
    text=str(value or '').strip()
    return text[:-2] if text.endswith('.0') and text[:-2].isdigit() else text

def _blocking(row):
    return row['status'] in {'Sending', 'OutcomeUnknown', 'Succeeded', 'FailedAfterSend'}

def was_sent_to_ai(resume_id,vacancy_id):
    with database() as con:
        row=con.execute('SELECT status,updated FROM history WHERE resume_id=? AND vacancy_id=?',(normalize(resume_id),normalize(vacancy_id))).fetchone()
    return bool(row and _blocking(row))

def reset_history_cache():pass

def reserve_for_ai(resume_id,vacancy_id,vacancy_name,resume_url,responsible_email,task_id, connection=None):
    key=(normalize(resume_id),normalize(vacancy_id));now=time.time()
    from contextlib import nullcontext
    with (nullcontext(connection) if connection is not None else database()) as con:
        if connection is None:con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM history WHERE resume_id=? AND vacancy_id=?',key).fetchone()
        if row and _blocking(row):return False
        data={'resume_id':key[0],'vacancy_id':key[1],'vacancy_name':vacancy_name,'resume_url':resume_url,'responsible_email':responsible_email,'task_id':task_id,'request_status':'Sending','ai_status':'Pending','email_status':'Pending','request_started_at':datetime.now().isoformat()}
        con.execute('INSERT INTO history VALUES(?,?,?,?,?) ON CONFLICT(resume_id,vacancy_id) DO UPDATE SET status=excluded.status,updated=excluded.updated,data=excluded.data',(*key,'Sending',now,json.dumps(data,ensure_ascii=False)))
    return True

def update_tracking(resume_id,vacancy_id,request_status,ai_status,total_score,match_level,email_status,notes='',response_received_at=None,email_event_at=None,result=None):
    key=(normalize(resume_id),normalize(vacancy_id))
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT data FROM history WHERE resume_id=? AND vacancy_id=?',key).fetchone()
        if not row:raise TrackerUnavailableError('Запись кандидата не найдена в SQLite.')
        data=json.loads(row['data']);data.update(request_status=request_status,ai_status=ai_status,total_score=total_score,match_level=match_level,email_status=email_status,notes=notes,response_received_at=response_received_at,email_event_at=email_event_at)
        if result is not None:data['result']=result
        con.execute('UPDATE history SET status=?,updated=?,data=? WHERE resume_id=? AND vacancy_id=?',(request_status,time.time(),json.dumps(data,ensure_ascii=False,default=str),*key))

def import_legacy_history(path):
    """One-time import of the existing workbook, without launching Excel."""
    from openpyxl import load_workbook
    path=__import__('pathlib').Path(path)
    if not path.is_file():return 0
    name='legacy_excel_v1'
    with database() as con:
        if con.execute('SELECT 1 FROM migrations WHERE name=?',(name,)).fetchone():return 0
    book=load_workbook(path,read_only=True,data_only=True)
    try:
        sheet=book['AI Resume History'];rows=list(sheet.iter_rows(values_only=True))
    finally:book.close()
    if not rows:return 0
    headers=[str(x or '').strip() for x in rows[0]];count=0
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT 1 FROM migrations WHERE name=?',(name,)).fetchone():return 0
        for row in rows[1:]:
            data=dict(zip(headers,row));rid=normalize(data.get('Resume ID'));vid=normalize(data.get('Vacancy ID'))
            if not rid or not vid:continue
            status=str(data.get('AI Request Status') or '')
            stamp=data.get('Updated At') or data.get('Timestamp')
            if isinstance(stamp,(int,float)):
                from openpyxl.utils.datetime import from_excel
                stamp=from_excel(stamp)
            if isinstance(stamp,str):
                try:stamp=datetime.fromisoformat(stamp)
                except ValueError:stamp=None
            updated=stamp.timestamp() if isinstance(stamp,datetime) else 0
            con.execute('INSERT INTO history VALUES(?,?,?,?,?) ON CONFLICT(resume_id,vacancy_id) DO NOTHING', (rid,vid,status,updated,json.dumps(data,ensure_ascii=False,default=str)));count+=1
        con.execute('INSERT INTO migrations VALUES(?,?)',(name,time.time()))
    return count

