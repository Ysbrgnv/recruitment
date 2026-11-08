"""PostgreSQL durable queue. One active job per task; process fencing via advisory locks."""
import os,time,uuid,threading,logging,signal
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
log=logging.getLogger(__name__)
_worker_context=threading.local()

def enabled():return os.getenv('TASK_QUEUE_BACKEND','local')=='postgres'

@contextmanager
def connect():
    url=os.environ.get('DATABASE_URL')
    if not url:raise RuntimeError('DATABASE_URL is required for the PostgreSQL queue')
    from db_pool import connection
    from transaction_scope import pg_connection,bind
    existing=pg_connection.get()
    if existing is not None:
        yield existing
        return
    with connection(url) as con:
        with bind(pg_connection,con):yield con

def init():
    if os.getenv("APP_SCHEMA_MANAGED")=="true":return
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS task_jobs (
          id BIGSERIAL PRIMARY KEY,task_id TEXT NOT NULL,phase TEXT NOT NULL,
          lane TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          owner TEXT,lease_until DOUBLE PRECISION,attempt INTEGER NOT NULL DEFAULT 0,
          created DOUBLE PRECISION NOT NULL,updated DOUBLE PRECISION NOT NULL,
          UNIQUE(task_id,phase))""")
        con.execute("ALTER TABLE task_jobs ADD COLUMN IF NOT EXISTS next_run DOUBLE PRECISION NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE task_jobs ADD COLUMN IF NOT EXISTS last_error TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS task_jobs_ready ON task_jobs(lane,state,created)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS task_jobs_one_running ON task_jobs(task_id) WHERE state='running'")

    from app.core.telemetry import init as init_metrics
    init_metrics()

def enqueue(task_id,prepare=False):
    from app_state import get_task
    task=get_task(task_id)
    if not task:raise KeyError(task_id)
    phase='prepare' if prepare else 'run'
    lane='browser' if task['mode']=='xray' and not prepare else 'general'
    now=time.time()
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(714202601)')
        current=con.execute('SELECT status FROM tasks WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
        if not current or current['status']!='queued':return False
        row=con.execute("""INSERT INTO task_jobs(task_id,phase,lane,created,updated)
          VALUES(%s,%s,%s,%s,%s) ON CONFLICT(task_id,phase) DO UPDATE
          SET state='pending',owner=NULL,lease_until=NULL,attempt=0,next_run=0,last_error=NULL,updated=EXCLUDED.updated
          WHERE task_jobs.state IN ('done','dead') RETURNING id""",(task_id,phase,lane,now,now)).fetchone()
    return bool(row)

def claim(lane,lease=45):
    now=time.time();owner=uuid.uuid4().hex
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(714202601)')
        if lane=='browser':
            con.execute('SELECT pg_advisory_xact_lock(714202604)')
            cap=max(1,min(8,int(os.getenv('BROWSER_MAX_SESSIONS','2'))))
            count=con.execute("SELECT COUNT(*) AS n FROM task_jobs WHERE lane='browser' AND state='running'").fetchone()['n']
            if count>=cap:return None
        row=con.execute("""SELECT j.* FROM task_jobs j JOIN tasks t ON t.id=j.task_id WHERE t.status='queued' AND j.lane=%s AND j.state='pending' AND j.next_run<=%s
          AND NOT EXISTS(SELECT 1 FROM task_jobs a WHERE a.task_id=j.task_id AND a.state='running')
          ORDER BY j.created,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1""",(lane,now)).fetchone()
        if not row:return None
        # The lock survives commits, but is automatically released if the worker dies.
        if not con.execute('SELECT pg_try_advisory_xact_lock(%s) AS ok',(row['id'],)).fetchone()['ok']:return None
        row=con.execute("""UPDATE task_jobs SET state='running',owner=%s,lease_until=%s,
          attempt=attempt+1,updated=%s WHERE id=%s RETURNING *""",(owner,now+lease,now,row['id'])).fetchone()
        from app_state import update_task
        update_task(row['task_id'],status='running',stage='query' if row['phase']=='prepare' else 'setup')
        return row

def heartbeat(job,lease=45):
    worker=getattr(_worker_context,'worker_id',None) or os.getenv('WORKER_ID')
    if worker:
        from app.core.telemetry import presence
        presence(worker,job['lane'],'busy')
    with connect() as con:
        return con.execute("UPDATE task_jobs SET lease_until=%s,updated=%s WHERE id=%s AND owner=%s AND state='running'",(time.time()+lease,time.time(),job['id'],job['owner'])).rowcount==1

def ack(job):
    with connect() as con:
        con.execute("UPDATE task_jobs SET state='done',owner=NULL,lease_until=NULL,updated=%s WHERE id=%s AND owner=%s AND state='running'",(time.time(),job['id'],job['owner']))

def finish_failure(job,retryable,reason):
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(714202601)')
        row=con.execute("SELECT id FROM task_jobs WHERE id=%s AND owner=%s AND state='running' FOR UPDATE",(job['id'],job['owner'])).fetchone()
        if not row:return
        _finish_failure(job,retryable,reason)

def _finish_failure(job,retryable,reason):
    from app_state import get_task,update_task
    import operation_store as ops
    from app.core.retry_policy import maximum,delay
    from app.core.telemetry import increment
    ops.mark_inflight_unknown(job['task_id'])
    if ops.unknowns(job['task_id']):
        update_task(job['task_id'],status='waiting_manual',stage='manual_resolution',message='Проверьте исход внешней операции.')
        ack(job);increment('manual_resolution_total');return
    if retryable and job['attempt']<maximum():
        state='pending';when=time.time()+delay(job['attempt'])
        update_task(job['task_id'],status='queued',stage='retry_wait',message='Повтор после временной ошибки',error=None,error_retryable=False,retry_at=when)
        increment('job_retries_total')
    else:
        state='dead';when=0
        update_task(job['task_id'],status='failed',stage='retry_exhausted' if retryable else 'failed',message='Задача остановлена; требуется ручное возобновление',finished=time.time())
        increment('job_dead_total')
    with connect() as con:
        con.execute('UPDATE task_jobs SET state=%s,next_run=%s,last_error=%s,owner=NULL,lease_until=NULL,updated=%s WHERE id=%s AND owner=%s',
            (state,when,reason[:1000],time.time(),job['id'],job['owner']))
    increment('job_errors_total')

def reap():
    """Never restart a live worker, even if its heartbeat was delayed."""
    from app_state import get_task,update_task
    import operation_store as ops
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(714202601)')
        rows=con.execute("SELECT * FROM task_jobs WHERE state='running' AND lease_until<%s FOR UPDATE SKIP LOCKED",(time.time(),)).fetchall()
        for job in rows:
            if not con.execute('SELECT pg_try_advisory_xact_lock(%s) AS ok',(job['id'],)).fetchone()['ok']:continue
            task=get_task(job['task_id'])
            if not task:
                con.execute("UPDATE task_jobs SET state='done',owner=NULL WHERE id=%s",(job['id'],));continue
            from shared_screen import stop_all
            stop_all(job['task_id'])
            ops.mark_inflight_unknown(job['task_id'])
            if ops.unknowns(job['task_id']):
                update_task(job['task_id'],status='waiting_manual',stage='manual_resolution',message='Обработчик перезапущен. Проверьте исход внешней операции.')
                state='done'
            elif task.get('watchdog_timeout'):
                update_task(job['task_id'],status='failed',stage='stage_timeout',error='Этап превысил тайм-аут. Проверьте сохранённый результат перед ручным возобновлением.',message='Задача остановлена watchdog',finished=time.time())
                state='dead'
            elif job['phase']=='prepare' and task['status']=='running':
                # Query generation predates the operation journal. A killed process
                # may already have sent a paid AI request; require an explicit resume.
                update_task(job['task_id'],status='waiting_manual',stage='manual_resolution',message='Подготовка запроса прервана. Возможен отправленный ИИ-запрос; возобновите задачу вручную после проверки.')
                state='done'
            elif task['status']=='waiting_browser':
                # A destroyed browser cannot preserve its original CAPTCHA.
                update_task(job['task_id'],status='failed',stage='browser_interrupted',browser_action=None,error='Браузерная сессия прервана. Сохранённые ссылки доступны; возобновите задачу для нового поиска.',message='Браузерная сессия прервана',finished=time.time())
                state='done'
            elif task['status'] in {'completed','failed','cancelled','expired','waiting_approval','waiting_manual'}:state='done'
            else:
                from app.core.retry_policy import maximum,delay
                if job['attempt']>=maximum():
                    update_task(job['task_id'],status='failed',stage='retry_exhausted',message='Исчерпан лимит восстановлений',finished=time.time());state='dead'
                else:
                    update_task(job['task_id'],status='queued',message='Восстановление после остановки обработчика');state='pending'
                    # Recovery has a short backoff too; never hot-loop a crashing task.
                    con.execute('UPDATE task_jobs SET next_run=%s WHERE id=%s',(time.time()+delay(job['attempt']),job['id']))
            con.execute('UPDATE task_jobs SET state=%s,owner=NULL,lease_until=NULL,updated=%s WHERE id=%s',(state,time.time(),job['id']))

def reconcile():
    # Repair the small create/submit crash window without resetting active jobs.
    from app_state import database,_read
    with database() as con:rows=con.execute("SELECT * FROM tasks WHERE status='queued'").fetchall()
    for row in rows:
        task=_read(row);phase='run' if task.get('query') else 'prepare'
        with connect() as con:
            exists=con.execute("SELECT 1 FROM task_jobs WHERE task_id=%s AND state IN ('pending','running')",(task['task_id'],)).fetchone()
        if not exists:enqueue(task['task_id'],phase=='prepare')

def run_once(lane,runner=None,lease=45):
    job=claim(lane,lease)
    if not job:return False
    # Claim/reaper race: recheck ownership while holding the session lock.
    from db_pool import connection_kwargs
    from psycopg.conninfo import conninfo_to_dict
    kwargs=connection_kwargs();kwargs['row_factory']=None
    kwargs['options']=conninfo_to_dict(os.environ['DATABASE_URL']).get('options','')+' '+kwargs['options']
    lock=psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,**kwargs)
    locked=lock.execute('SELECT pg_try_advisory_lock(%s)',(job['id'],)).fetchone()[0]
    if not locked:lock.close();return False
    stop=threading.Event()
    def beat():
        while not stop.wait(max(1,lease/3)):
            try:
                lock.execute('SELECT 1')
                if not heartbeat(job,lease):
                    with connect() as con:
                        row=con.execute('SELECT state,owner FROM task_jobs WHERE id=%s',(job['id'],)).fetchone()
                    if row and row['state'] in {'done','dead','pending'} and row['owner'] is None:return
                    raise RuntimeError('Job ownership lost')
            except Exception:
                log.exception('Worker fencing: stopping process after database/lease loss')
                os._exit(70)
    thread=None;watch_thread=None
    try:
        if not heartbeat(job,lease):return False
        thread=threading.Thread(target=beat,daemon=True);thread.start()
        from progress_watchdog import watch
        watch_thread=threading.Thread(target=watch,args=(job,stop),name='progress-watchdog',daemon=True);watch_thread.start()
        if runner is None:
            from web_server import run_task
            runner=run_task
        log.info('Job started',extra={'task_id':job['task_id'],'job_id':job['id'],'attempt':job['attempt'],'worker_id':job['owner']})
        from transaction_scope import worker_job,bind
        with bind(worker_job,job):
            runner(job['task_id'],job['phase']=='prepare')
        from app_state import get_task
        task=get_task(job['task_id'])
        if task and task['status']=='failed':finish_failure(job,bool(task.get('error_retryable')),'task_failed')
        else:
            ack(job)
            from app.core.telemetry import increment
            if task and task['status']=='completed':increment('jobs_completed_total')
            elif task and task['status']=='waiting_manual':increment('manual_resolution_total')
    except Exception as exc:
        log.exception('Worker failed',extra={'task_id':job['task_id'],'job_id':job['id']})
        from app.core.retry_policy import transient
        finish_failure(job,transient(exc),type(exc).__name__)
    finally:
        stop.set()
        if thread:thread.join(timeout=5)
        if watch_thread:watch_thread.join(timeout=5)
        lock.close()
    return True

def worker_loop(lane,stop,index=1):
    worker=uuid.uuid4().hex
    _worker_context.worker_id=worker
    from app.core.telemetry import presence
    try:
        while not stop.is_set():
            try:
                presence(worker,lane)
                reap();reconcile()
                if not run_once(lane):stop.wait(1)
            except Exception:
                log.exception('Queue iteration failed',extra={'worker_id':worker})
                stop.wait(3)
    finally:
        try:presence(worker,lane,'stopped')
        except Exception:log.exception('Cannot update stopped worker state',extra={'worker_id':worker})


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--lane',choices=['general','browser'],required=True)
    args=parser.parse_args();init()
    stop=threading.Event()
    def shutdown(*_):stop.set()
    for sig in [signal.SIGINT,signal.SIGTERM]+([signal.SIGBREAK] if os.name=="nt" else []):signal.signal(sig,shutdown)
    requested=os.getenv('GENERAL_CONSUMERS','16') if args.lane=='general' else os.getenv('XRAY_CONSUMERS','8')
    maximum=16 if args.lane=='general' else 8
    try:count=max(1,min(maximum,int(requested)))
    except (TypeError,ValueError):count=maximum
    log.info('Starting parallel consumers',extra={'stage':args.lane,'attempt':count})
    threads=[threading.Thread(target=worker_loop,args=(args.lane,stop,i+1),name=f'{args.lane}-{i+1}',daemon=False) for i in range(count)]
    from db_pool import close_pools
    try:
        for thread in threads:thread.start()
        while not stop.wait(1):
            if any(not thread.is_alive() for thread in threads):
                log.error('Consumer thread stopped unexpectedly; stopping worker process')
                stop.set()
    finally:
        stop.set()
        for thread in threads:thread.join(timeout=10)
        close_pools()

def sync_terminal(con,task):
    """Called before task save: task and owned queue transition commit together.
    con is the legacy adapter over the SAME PostgreSQL transaction.
    """
    from transaction_scope import worker_job
    job=worker_job.get()
    if not job or task['task_id']!=job['task_id']:return
    status=task['status']
    if status not in {'completed','failed','waiting_manual','waiting_approval','cancelled','expired'}:return
    row=con.execute("SELECT * FROM task_jobs WHERE id=? AND owner=? AND state='running' FOR UPDATE",(job['id'],job['owner'])).fetchone()
    if not row:raise RuntimeError('Job ownership lost before task transition')
    state='done';when=0;reason=None
    if status=='failed':
        con.execute("UPDATE operations SET state='unknown',updated=? WHERE task_id=? AND state='sending'",(time.time(),task['task_id']))
        unknown=con.execute("SELECT 1 FROM operations WHERE task_id=? AND state='unknown' LIMIT 1",(task['task_id'],)).fetchone()
        if unknown:
            task.update(status='waiting_manual',stage='manual_resolution',message='Проверьте исход внешней операции.')
        elif task.get('error_retryable'):
            from app.core.retry_policy import maximum,delay
            if row['attempt']<maximum():
                state='pending';when=time.time()+delay(row['attempt'])
                task.update(status='queued',stage='retry_wait',message='Повтор после временной ошибки',error=None,error_retryable=False,retry_at=when)
                task.pop('finished',None)
            else:state='dead';task['stage']='retry_exhausted'
        else:state='dead'
        reason='task_failed'
        if task['status']!='waiting_manual':
            for name in ('job_errors_total','job_retries_total' if state=='pending' else 'job_dead_total'):
                con.execute("INSERT INTO service_counters(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=service_counters.value+1",(name,))
    con.execute("UPDATE task_jobs SET state=?,next_run=?,last_error=?,owner=NULL,lease_until=NULL,updated=? WHERE id=? AND owner=? AND state='running'",(state,when,reason,time.time(),job['id'],job['owner']))

if __name__=='__main__':main()

