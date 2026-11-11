"""Durable task state; only disposable HH caches are held in memory."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from app.core.settings import BASE_DIR, MAX_BACKGROUND_WORKERS, TASK_RETENTION_HOURS

DB_PATH = Path(os.getenv("RECRUITMENT_DB_PATH", str(BASE_DIR / "recruitment.db")))
APP_STATE = {"vacancies": [], "vacancy_cache": {}, "startup_error": None}
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_BACKGROUND_WORKERS, thread_name_prefix="recruitment")
REVIEW_TTL = max(60, int(os.getenv("QUERY_REVIEW_TTL_SECONDS", "86400")))

@contextmanager
def database():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        from app.persistence.pg_backend import database as pg_database
        with pg_database(url) as con:
            yield con
        return
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    if os.getenv('APP_SCHEMA_MANAGED')=='true':return
    with database() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, vacancy_id TEXT NOT NULL, email TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, expires REAL, data TEXT NOT NULL
        );
        DROP INDEX IF EXISTS one_active_task;
        CREATE TABLE IF NOT EXISTS history (
            resume_id TEXT NOT NULL, vacancy_id TEXT NOT NULL,
            status TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(resume_id,vacancy_id)
        );
        CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, applied REAL NOT NULL);
        """)

def _read(row):
    if row is None: return None
    data=json.loads(row['data'])
    data.update(task_id=row['id'],status=row['status'],created=row['created'],updated=row['updated'],expires=row['expires'])
    return data

def _save(con, task):
    if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        from app.persistence.durable_queue import sync_terminal
        sync_terminal(con,task)
    now=time.time()
    con.execute("UPDATE tasks SET status=?,updated=?,expires=?,data=? WHERE id=?",
                (task['status'],now,task.get('expires'),json.dumps(task,ensure_ascii=False,default=str),task['task_id']))

def expire_reviews():
    now=time.time()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        for row in con.execute("SELECT * FROM tasks WHERE status='waiting_approval' AND expires<=?",(now,)).fetchall():
            task=_read(row)
            task.update(status='expired',stage='expired',message='Срок подтверждения запроса истёк',error='Создайте новый запрос.',finished=now)
            _save(con,task)

def create_task(task_id,vacancy_id,count,email,mode,owner_session=None):
    expire_reviews()
    now=time.time(); email=email.strip().casefold()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        rows=con.execute("SELECT * FROM tasks WHERE vacancy_id=? AND email=? AND mode=? AND status IN ('queued','running','waiting_approval','waiting_manual','waiting_browser')",(vacancy_id,email,mode)).fetchall()
        for row in rows:
            existing=_read(row)
            if existing.get('owner_session')==owner_session:return existing['task_id'],False
        task={'task_id':task_id,'vacancy_id':vacancy_id,'responsible_email':email,'mode':mode,'requested_count':count,'status':'queued','stage':'setup','message':'Задача поставлена в очередь','progress':{'requested_count':count},'result':None,'error':None,'created':now,'expires':None,'owner_session':owner_session}
        con.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',(task_id,vacancy_id,email,mode,'queued',now,now,None,json.dumps(task,ensure_ascii=False)))
    return task_id,True

def get_task(task_id):
    expire_reviews()
    with database() as con:return _read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

def update_task(task_id,**fields):
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if task is None:raise KeyError(task_id)
        task.update(fields);_save(con,task)

def set_review(task_id,query,vacancy_search_profile=None):
    fields={'status':'waiting_approval','stage':'query_review','query':query,'expires':time.time()+REVIEW_TTL,'message':'Проверьте поисковый запрос','progress':{'query':query}}
    if vacancy_search_profile is not None:fields['vacancy_search_profile']=vacancy_search_profile
    update_task(task_id,**fields)

def save_query(task_id,query):
    expire_reviews()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if not task or task['status']!='waiting_approval':return False
        task['query']=query;task['progress']['query']=query;_save(con,task)
    return True

def claim_review(task_id):
    expire_reviews()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if not task or task['status']!='waiting_approval':return False
        task.update(status='queued',stage='setup',message='Поиск поставлен в очередь',expires=None)
        _save(con,task)
        if os.getenv("TASK_QUEUE_BACKEND","local")=="postgres":
            from app.persistence.durable_queue import enqueue
            enqueue(task_id,False)
    return True

def cancel_review(task_id):
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if not task or task['status']!='waiting_approval':return False
        task.update(status='cancelled',stage='cancelled',message='Запрос отменён',finished=time.time());_save(con,task)
    return True

def recover_tasks():
    from app.persistence.operation_store import recover
    return recover()

def list_tasks(vacancy_id, owner_session=None):
    expire_reviews()
    with database() as con:
        if owner_session is None:
            return [_read(row) for row in con.execute('SELECT * FROM tasks WHERE vacancy_id=? ORDER BY created DESC LIMIT 20',(vacancy_id,))]
        # Apply ownership before the display limit. Stream rows to avoid
        # requiring SQLite JSON extensions or changing the database schema.
        from app.core.api_guard import owns
        result=[]
        for row in con.execute('SELECT * FROM tasks WHERE vacancy_id=? ORDER BY created DESC',(vacancy_id,)):
            task=_read(row)
            if owns(task,owner_session):
                result.append(task)
                if len(result)==20:break
        return result
