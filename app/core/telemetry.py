"""Low-cardinality Prometheus metrics. No candidate data, task IDs or tokens."""
import os,time,logging
log=logging.getLogger(__name__)

def init():
    if os.getenv('APP_SCHEMA_MANAGED')=='true':return
    from app.persistence.durable_queue import connect
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS service_counters(
          name TEXT PRIMARY KEY,value DOUBLE PRECISION NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS worker_presence(
          worker_id TEXT PRIMARY KEY,lane TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,
          state TEXT NOT NULL)""")

def increment(name,value=1):
    try:
        from app.persistence.durable_queue import connect
        with connect() as c:
            c.execute("""INSERT INTO service_counters(name,value) VALUES(%s,%s)
              ON CONFLICT(name) DO UPDATE SET value=service_counters.value+EXCLUDED.value""",(name,value))
    except Exception:log.exception('Metrics update failed')

def observe_captcha(seconds):
    increment('captcha_wait_seconds_sum',seconds);increment('captcha_wait_seconds_count')
    for bound in (10,30,60,300,900,1800,3600):
        if seconds<=bound:increment('captcha_wait_bucket_'+str(bound))

def presence(worker_id,lane,state='idle'):
    from app.persistence.durable_queue import connect
    with connect() as c:
        c.execute("""INSERT INTO worker_presence VALUES(%s,%s,%s,%s)
          ON CONFLICT(worker_id) DO UPDATE SET updated=EXCLUDED.updated,state=EXCLUDED.state""",
          (worker_id,lane,time.time(),state))

def render():
    from app.persistence.durable_queue import connect
    now=time.time();lines=[]
    with connect() as c:
        for r in c.execute('SELECT lane,state,COUNT(*) AS n FROM task_jobs GROUP BY lane,state'):
            lines.append('recruitment_queue_jobs{lane="'+r['lane']+'",state="'+r['state']+'"} '+str(r['n']))
        r=c.execute("SELECT COALESCE(MAX(%s-created),0) AS age FROM task_jobs WHERE state='pending'",(now,)).fetchone()
        lines.append('recruitment_queue_oldest_pending_seconds '+str(max(0,r['age'])))
        for r in c.execute('SELECT status,COUNT(*) AS n FROM tasks GROUP BY status'):
            lines.append('recruitment_tasks{status="'+r['status']+'"} '+str(r['n']))
        r=c.execute("SELECT COUNT(*) AS n FROM task_jobs WHERE state='running' AND lease_until<%s",(now,)).fetchone()
        lines.append('recruitment_jobs_expired_lease '+str(r['n']))
        r=c.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='running' AND updated<%s",(now-int(os.getenv('STUCK_AFTER_SECONDS','600')),)).fetchone()
        lines.append('recruitment_tasks_no_progress '+str(r['n']))
        r=c.execute("SELECT COUNT(*) AS n,COALESCE(MAX(%s-updated),0) AS age FROM tasks WHERE status='waiting_browser'",(now,)).fetchone()
        lines.extend(['recruitment_captcha_waiting '+str(r['n']),'recruitment_captcha_oldest_wait_seconds '+str(max(0,r['age']))])
        for r in c.execute('SELECT lane,COUNT(*) AS n FROM worker_presence WHERE updated>%s AND state<>%s GROUP BY lane',(now-60,'stopped')):
            lines.append('recruitment_workers_alive{lane="'+r['lane']+'"} '+str(r['n']))
        counters={r['name']:r['value'] for r in c.execute('SELECT * FROM service_counters')}
    for name,value in sorted(counters.items()):
        if name.startswith('captcha_wait_bucket_'):continue
        lines.append('recruitment_'+name+' '+str(value))
    for bound in (10,30,60,300,900,1800,3600):
        lines.append('recruitment_captcha_wait_seconds_bucket{le="'+str(bound)+'"} '+str(counters.get('captcha_wait_bucket_'+str(bound),0)))
    lines.append('recruitment_captcha_wait_seconds_bucket{le="+Inf"} '+str(counters.get('captcha_wait_seconds_count',0)))
    return '\n'.join(lines)+'\n'

