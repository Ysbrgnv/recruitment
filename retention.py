"""Purge expired terminal task payloads, retain minimal deduplication keys.
Never purge active/manual tasks or ambiguous external effects.
"""
import json,time,threading,logging
from app.persistence.app_state import database,expire_reviews
from app.core.settings import TASK_RETENTION_HOURS
logger=logging.getLogger(__name__)
def cleanup(now=None):
    now=time.time() if now is None else now
    cutoff=now-TASK_RETENTION_HOURS*3600;removed=[]
    expire_reviews()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history_rows=con.execute('SELECT resume_id,vacancy_id,status,data FROM history').fetchall()
        history_by_task={}
        for h in history_rows:
            data=json.loads(h['data']);history_by_task.setdefault(data.get('task_id'),[]).append((h,data))
        rows=con.execute("SELECT id FROM tasks WHERE status IN ('completed','cancelled','expired','failed','interrupted') AND updated<?",(cutoff,)).fetchall()
        for row in rows:
            tid=row['id']
            if 'operations' in tables and con.execute("SELECT 1 FROM operations WHERE task_id=? AND state IN ('sending','unknown','retry_allowed') LIMIT 1",(tid,)).fetchone():continue
            linked=history_by_task.get(tid,[])
            if any(h['status'] in {'Sending','OutcomeUnknown','FailedAfterSend'} or data.get('email_status')=='OutcomeUnknown' for h,data in linked):continue
            for h,data in linked:
                minimal={key:data[key] for key in ('resume_id','vacancy_id','request_status','ai_status','email_status') if key in data}
                minimal['payload_purged_at']=now
                con.execute('UPDATE history SET data=? WHERE resume_id=? AND vacancy_id=?',(json.dumps(minimal),h['resume_id'],h['vacancy_id']))
            if 'resolution_audit' in tables:con.execute('DELETE FROM resolution_audit WHERE task_id=?',(tid,))
            if 'operations' in tables:con.execute('DELETE FROM operations WHERE task_id=?',(tid,))
            con.execute('DELETE FROM tasks WHERE id=?',(tid,));removed.append(tid)
        # Imported/orphaned history: remove old payload, never delete dedup keys/status.
        for h in con.execute('SELECT resume_id,vacancy_id,status,updated,data FROM history WHERE updated<?',(cutoff,)).fetchall():
            data=json.loads(h['data']);tid=data.get('task_id')
            if h['status'] in {'Sending','OutcomeUnknown','FailedAfterSend'} or data.get('email_status')=='OutcomeUnknown':continue
            if tid and con.execute('SELECT 1 FROM tasks WHERE id=?',(tid,)).fetchone():continue
            if data.get('payload_purged_at'):continue
            minimal={key:data[key] for key in ('resume_id','vacancy_id','request_status','ai_status','email_status') if key in data};minimal['payload_purged_at']=now
            con.execute('UPDATE history SET data=? WHERE resume_id=? AND vacancy_id=?',(json.dumps(minimal),h['resume_id'],h['vacancy_id']))
    if __import__('os').getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        with database() as con:
            con.execute("DELETE FROM task_jobs WHERE state IN ('done','dead') AND NOT EXISTS(SELECT 1 FROM tasks WHERE tasks.id=task_jobs.task_id)")
            con.execute("DELETE FROM browser_screens WHERE NOT EXISTS(SELECT 1 FROM tasks WHERE tasks.id=browser_screens.task_id AND tasks.status='waiting_browser')")
            con.execute('DELETE FROM worker_presence WHERE updated<?',(now-86400,))
    return removed

def start_retention():
    stop=threading.Event()
    def run():
        while not stop.is_set():
            try:cleanup()
            except Exception:logger.exception('Task retention cleanup failed')
            stop.wait(60)
    thread=threading.Thread(target=run,name='task-retention',daemon=True);thread.start()
    return stop,thread

