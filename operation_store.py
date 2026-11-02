"""Durable external-operation journal. No automatic retry of ambiguous effects."""
import json,time,uuid
from app_state import database,get_task,update_task

def init():
    import os
    if os.getenv('APP_SCHEMA_MANAGED')=='true':return
    with database() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS operations(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
          item_key TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
          updated REAL NOT NULL, UNIQUE(task_id,kind,item_key));
        CREATE TABLE IF NOT EXISTS resolution_audit(
          id INTEGER PRIMARY KEY, operation_id TEXT, task_id TEXT,
          action TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS xray_history(
          vacancy_id TEXT NOT NULL,url TEXT NOT NULL,task_id TEXT NOT NULL,
          status TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(vacancy_id,url));
        """)

def decode(row):
    if not row:return None
    d=dict(row);d['data']=json.loads(d['data']);return d

def get_op(task_id,kind,key):
    with database() as con:return decode(con.execute('SELECT * FROM operations WHERE task_id=? AND kind=? AND item_key=?',(task_id,kind,key)).fetchone())

def begin(task_id,kind,key,data=None, connection=None):
    from contextlib import nullcontext
    with (nullcontext(connection) if connection is not None else database()) as con:
        if connection is None:con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM operations WHERE task_id=? AND kind=? AND item_key=?',(task_id,kind,key)).fetchone()
        if row and row['state']!='retry_allowed':raise RuntimeError('Operation is not available for execution')
        oid=row['id'] if row else uuid.uuid4().hex
        con.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,kind,item_key) DO UPDATE SET state='sending',data=excluded.data,updated=excluded.updated",(oid,task_id,kind,key,'sending',json.dumps(data or {},ensure_ascii=False,default=str),time.time()))
    return oid

def finish(oid,state,data):
    import os
    from transaction_scope import worker_job
    job=worker_job.get()
    with database() as con:
        if job is not None and os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
            con.execute('BEGIN IMMEDIATE')
            owned=con.execute("SELECT id FROM task_jobs WHERE id=? AND owner=? AND state='running' FOR UPDATE",(job['id'],job['owner'])).fetchone()
            if not owned:raise RuntimeError('Operation ownership lost before completion; stale worker result rejected by fencing.')
        con.execute('UPDATE operations SET state=?,data=?,updated=? WHERE id=?',(state,json.dumps(data,ensure_ascii=False,default=str),time.time(),oid))

def unknowns(task_id):
    with database() as con:
        rows=con.execute("SELECT * FROM operations WHERE task_id=? AND state IN ('unknown','sending')",(task_id,)).fetchall()
    return [decode(x) for x in rows]

def recover():
    """Called once before workers start. Preserve completed results, pause ambiguity."""
    init()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE operations SET state='unknown',updated=? WHERE state='sending'",(time.time(),))
        # Older versions had no operation journal. Conservatively migrate uncertain reservations.
        for row in con.execute("SELECT * FROM history WHERE status IN ('Sending','FailedAfterSend')").fetchall():
            data=json.loads(row['data']);tid=data.get('task_id')
            con.execute("UPDATE history SET status='OutcomeUnknown' WHERE resume_id=? AND vacancy_id=?",(row['resume_id'],row['vacancy_id']))
            if tid and con.execute('SELECT 1 FROM tasks WHERE id=?',(tid,)).fetchone():
                if not con.execute("SELECT 1 FROM operations WHERE task_id=? AND kind IN ('ai','legacy_ai') AND item_key=?",(tid,row['resume_id'])).fetchone():
                    con.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,tid,'legacy_ai',row['resume_id'],'unknown',json.dumps(data,default=str),time.time()))
        rows=con.execute("SELECT id FROM tasks WHERE status IN ('queued','running','interrupted','waiting_manual','waiting_browser')").fetchall()
    runnable=[]
    for row in rows:
        tid=row['id']
        if unknowns(tid):update_task(tid,status='waiting_manual',stage='manual_resolution',message='Исход операции неизвестен. Требуется ручная проверка.')
        else:
            update_task(tid,status='queued',stage='setup',message='Восстановление с сохранённого этапа')
            runnable.append(tid)
    return runnable

def resolve(task_id,oid,action,reason):
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Укажите основание ручного решения.')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT * FROM operations WHERE id=? AND task_id=? AND state='unknown'",(oid,task_id)).fetchone()
        if not row:raise ValueError('Операция уже разрешена или недоступна.')
        allowed={'retry','skip'} if row['kind'] in {'ai','legacy_ai'} else {'retry','confirm_sent'}
        if action not in allowed:raise ValueError('Недопустимое действие.')
        state='retry_allowed' if action=='retry' else 'skipped' if action=='skip' else 'succeeded'
        data=json.loads(row['data'])
        if action=='confirm_sent':data['status']='Sent';data['manually_confirmed']=True
        con.execute('UPDATE operations SET state=?,data=?,updated=? WHERE id=?',(state,json.dumps(data,ensure_ascii=False,default=str),time.time(),oid))
        con.execute('INSERT INTO resolution_audit(operation_id,task_id,action,reason,created) VALUES(?,?,?,?,?)',(oid,task_id,action,reason.strip(),time.time()))
        task_row=con.execute('SELECT vacancy_id FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row['kind'] in {'ai','legacy_ai'}:
            status='RetryAllowed' if action=='retry' else 'OutcomeUnknown'
            con.execute('UPDATE history SET status=?,updated=? WHERE resume_id=? AND vacancy_id=?',(status,time.time(),row['item_key'],task_row['vacancy_id']))
    return True

def reserve_profile(vacancy_id,url,task_id):
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        from urllib.parse import urlparse
        parsed=urlparse(url)
        if parsed.hostname in {'hh.kz','hh.ru'}:
            rid=parsed.path.rsplit('/',1)[-1]
            known=con.execute('SELECT status FROM history WHERE resume_id=? AND vacancy_id=?',(rid,vacancy_id)).fetchone()
            if known and known['status'] in {'Sending','OutcomeUnknown','Succeeded','FailedAfterSend'}:return False
        row=con.execute('SELECT task_id,status FROM xray_history WHERE vacancy_id=? AND url=?',(vacancy_id,url)).fetchone()
        if row:
            if row['task_id']==task_id:return True
            # Release only a known-unsent selection from a terminated task.
            # Orphan, active, displayed, sent and ambiguous outcomes remain blocked.
            previous=con.execute('SELECT status FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
            op=con.execute("SELECT state FROM operations WHERE task_id=? AND kind='xray_email' AND item_key='digest'",(row['task_id'],)).fetchone()
            safe=row['status']=='Selected' and previous and previous['status'] in {'failed','cancelled','expired'} and (not op or op['state']=='retry_allowed')
            if not safe:return False
            con.execute("UPDATE xray_history SET task_id=?,status='Selected',updated=? WHERE vacancy_id=? AND url=?",(task_id,time.time(),vacancy_id,url))
            con.execute('INSERT INTO resolution_audit(task_id,action,reason,created) VALUES(?,?,?,?)',(task_id,'reclaim_unsent_selection','Previous task terminated before any email attempt: '+row['task_id'],time.time()))
            return True
        con.execute('INSERT INTO xray_history VALUES(?,?,?,?,?)',(vacancy_id,url,task_id,'Selected',time.time()))
    return True

def mark_profiles(task_id,status):
    with database() as con:con.execute('UPDATE xray_history SET status=?,updated=? WHERE task_id=?',(status,time.time(),task_id))

init()


def mark_inflight_unknown(task_id):
    with database() as con:
        con.execute("UPDATE operations SET state='unknown',updated=? WHERE task_id=? AND state='sending'",(time.time(),task_id))




def reserve_and_begin_ai(task_id, resume_id, vacancy_id, vacancy_name, resume_url, email):
    """History reservation and operation journal commit in one transaction."""
    from tracking_service import reserve_for_ai
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if not reserve_for_ai(resume_id, vacancy_id, vacancy_name, resume_url,
                              email, task_id, connection=con):
            return None
        return begin(task_id, 'ai', resume_id, {'resume_id': resume_id}, connection=con)




def record_ai_before_send_failure(oid,task_id,resume_id,vacancy_id,message):
    """Record proof of non-send and history together; a crash cannot release half a pair."""
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT * FROM operations WHERE id=? AND task_id=? AND kind='ai' AND state='sending'",(oid,task_id)).fetchone()
        history=con.execute('SELECT data FROM history WHERE resume_id=? AND vacancy_id=?',(resume_id,vacancy_id)).fetchone()
        if not row or not history:raise RuntimeError('AI failure record missing')
        data=json.loads(history['data'])
        if data.get('task_id')!=task_id:raise RuntimeError('History ownership changed')
        data.update(request_status='FailedBeforeSend',ai_status='Failed',email_status='Not sent',notes=message)
        con.execute("UPDATE operations SET state='failed',data=?,updated=? WHERE id=?",(json.dumps({'resume_id':resume_id,'error':message,'request_sent':False},ensure_ascii=False),time.time(),oid))
        con.execute("UPDATE history SET status='FailedBeforeSend',data=?,updated=? WHERE resume_id=? AND vacancy_id=?",(json.dumps(data,ensure_ascii=False,default=str),time.time(),resume_id,vacancy_id))


def allow_confirmed_unsent_retries(task_id,connection):
    """Only explicit task resume calls this, inside its transaction. Never release unknowns."""
    con=connection
    task=con.execute('SELECT vacancy_id FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:raise ValueError('Task not found')
    count=0
    for row in con.execute("SELECT * FROM operations WHERE task_id=? AND kind='ai' AND state='failed'",(task_id,)).fetchall():
        payload=json.loads(row['data'])
        if payload.get('request_sent') is not False:continue
        h=con.execute('SELECT status,data FROM history WHERE resume_id=? AND vacancy_id=?',(row['item_key'],task['vacancy_id'])).fetchone()
        if not h or h['status']!='FailedBeforeSend' or json.loads(h['data']).get('task_id')!=task_id:continue
        con.execute("UPDATE operations SET state='retry_allowed',updated=? WHERE id=?",(time.time(),row['id']))
        con.execute('INSERT INTO resolution_audit(operation_id,task_id,action,reason,created) VALUES(?,?,?,?,?)',(row['id'],task_id,'retry_confirmed_unsent','Explicit task resume; request_sent=False and matching FailedBeforeSend history',time.time()))
        count+=1
    return count
