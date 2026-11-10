"""Independent worker watchdog. A heartbeat is not evidence of business progress.
On timeout fence by killing this worker; recovery happens only after its DB lock dies.
No Python thread cancellation, and no retry of ambiguous external effects.
"""
import hashlib,json,logging,os,time
log=logging.getLogger(__name__)

def seconds(name,default,minimum=1,maximum=86400):
    try:value=float(os.getenv(name,str(default)))
    except (ValueError,TypeError):value=default
    if not __import__('math').isfinite(value):value=default
    return max(minimum,min(maximum,value))

def stage_limit(stage):
    if stage in {'query','ai'}:
        return seconds('STAGE_'+stage.upper()+'_TIMEOUT_SECONDS',seconds('NANO_TIMEOUT',300)+60)
    if stage=='email':return seconds('STAGE_EMAIL_TIMEOUT_SECONDS',180)
    if stage in {'search','history','pool_ready'}:return seconds('STAGE_SEARCH_TIMEOUT_SECONDS',1800)
    return seconds('STAGE_SETUP_TIMEOUT_SECONDS',120)

class ProgressClock:
    def __init__(self,now=None):
        now=time.monotonic() if now is None else now
        self.started=now;self.stage_started=now;self.last_progress=now
        self.key=None;self.signature=None;self.paused=False
    def check(self,task,operations=(),now=None,wall=None):
        now=time.monotonic() if now is None else now
        wall=time.time() if wall is None else wall
        status=task.get('status');stage=task.get('stage','setup')
        sending=[op for op in operations if op['state']=='sending']
        if any(op['kind'] in {'hh_email','xray_email'} for op in sending):stage='email'
        elif any(op['kind']=='ai' for op in sending):stage='ai'
        if status in {'completed','failed','waiting_manual','waiting_approval','cancelled','expired'}:return None
        if status=='waiting_browser':
            self.paused=True
            # Human waiting has its own fixed deadline; screenshots do not extend it.
            deadline=task.get('browser_deadline')
            if not isinstance(deadline,(int,float)):
                deadline=task.get('updated',wall)+seconds('CAPTCHA_WAIT_SECONDS',1800,60,3600)
            return 'browser_deadline' if wall>deadline+10 else None
        if self.paused:
            self.stage_started=now;self.last_progress=now;self.key=None;self.paused=False
        active=tuple(sorted((op['id'],op['kind']) for op in operations if op['state']=='sending'))
        key=(stage,active)
        if key!=self.key:self.key=key;self.stage_started=now
        # Ignore updated/message/lease and browser frame writes. Only persisted
        # checkpoint, progress counters, stage or operation state count as progress.
        value=(key,task.get('checkpoint'),task.get('progress'),
               tuple(sorted((op['id'],op['state']) for op in operations)))
        signature=hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).digest()
        if signature!=self.signature:self.signature=signature;self.last_progress=now
        if now-self.started>seconds('JOB_HARD_TIMEOUT_SECONDS',14400):return 'job_deadline'
        if now-self.stage_started>stage_limit(stage):return 'stage_deadline:'+stage
        if now-self.last_progress>seconds('STUCK_AFTER_SECONDS',600):return 'no_progress:'+stage
        return None

def terminate_worker(code=71):
    # Kill only this worker's driver descendants, never the shared BrowserServer.
    try:
        import psutil
        for child in psutil.Process().children(recursive=True):
            try:child.kill()
            except psutil.Error:pass
    finally:os._exit(code)

def record_timeout(job,reason):
    from app.persistence.durable_queue import connect
    from app.persistence.app_state import get_task,update_task
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(714202601)')
        row=con.execute("SELECT id FROM task_jobs WHERE id=%s AND owner=%s AND state='running' FOR UPDATE",(job['id'],job['owner'])).fetchone()
        if not row:return False
        task=get_task(job['task_id'])
        if not task or task['status'] in {'completed','failed','waiting_manual','waiting_approval','cancelled','expired'}:return False
        update_task(job['task_id'],watchdog_timeout={'reason':reason,'at':time.time()},message='Watchdog: обработчик остановлен по тайм-ауту; ожидается безопасное восстановление.')
        # Expire the lease, but keep state/owner until process death releases fencing.
        con.execute('UPDATE task_jobs SET lease_until=0,last_error=%s WHERE id=%s AND owner=%s',(reason,job['id'],job['owner']))
    return True

def watch(job,stop):
    from app.persistence.durable_queue import connect
    from app.persistence.app_state import _read
    clock=ProgressClock();interval=seconds('WATCHDOG_POLL_SECONDS',2,.1,10)
    while not stop.wait(interval):
        try:
            with connect() as con:
                owned=con.execute("SELECT 1 FROM task_jobs WHERE id=%s AND owner=%s AND state='running'",(job['id'],job['owner'])).fetchone()
                if not owned:return
                row=con.execute('SELECT * FROM tasks WHERE id=%s',(job['task_id'],)).fetchone()
                operations=con.execute('SELECT id,kind,state FROM operations WHERE task_id=%s',(job['task_id'],)).fetchall()
            reason=clock.check(_read(row) or {},operations)
            if reason:
                if stop.is_set():return
                if record_timeout(job,reason):
                    log.error('Progress watchdog timeout',extra={'task_id':job['task_id'],'job_id':job['id'],'stage':reason})
                    terminate_worker()
                return
        except Exception:
            log.exception('Progress watchdog database check failed')
            # Lease fencing thread also stops on DB loss; hard limit remains bounded.
            if time.monotonic()-clock.started>seconds('JOB_HARD_TIMEOUT_SECONDS',14400):terminate_worker()

