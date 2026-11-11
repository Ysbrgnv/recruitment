"""Container-only adapter. Existing application entrypoints remain unchanged."""
import os,sys,runpy,subprocess,time,signal
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
def secret(name):
    value=Path('/run/secrets',name).read_text(encoding='utf-8').strip()
    if not value:raise SystemExit('Empty secret: '+name)
    return value
def dsn(user,password):
    from psycopg.conninfo import make_conninfo
    return make_conninfo(host=os.getenv('DB_HOST','db'),port=os.getenv('DB_PORT','5432'),
        dbname=os.getenv('DB_NAME','recruitment'),user=user,password=password)
def runtime():
    os.environ.update(DATABASE_URL=dsn('recruitment_app',secret('db_app')),
        TASK_QUEUE_BACKEND='postgres',APP_SCHEMA_MANAGED='true',HH_INTERACTIVE_AUTH='false',
        BROWSER_WS_ENDPOINT='ws://browser:3000/'+secret('browser_key'),
        BROWSER_HEALTH_URL='http://browser:3001/health/ready',
        METRICS_TOKEN=secret('metrics_token'),GMAIL_APP_PASSWORD_FILE='/run/secrets/gmail_password')
def admin():
    os.environ.update(PG_ADMIN_URL=dsn('postgres',secret('db_admin')),
        PG_BACKUP_URL=dsn('recruitment_backup',secret('db_backup')))
def execute(script,args=()):
    sys.argv=[script,*args];runpy.run_path(str(ROOT/script),run_name='__main__')
def main():
    role=sys.argv[1] if len(sys.argv)>1 else 'web'
    if role in {'web','general','browser','check-gmail','smoke-browser'}:
        runtime()
        if role=='web':execute('entrypoints/web.py')
        elif role in {'general','browser'}:execute('entrypoints/worker.py',['--lane',role])
        elif role=='check-gmail':execute('scripts/diagnostics/check_gmail.py')
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                a=p.chromium.connect(os.environ['BROWSER_WS_ENDPOINT'])
                b=p.chromium.connect(os.environ['BROWSER_WS_ENDPOINT'])
                try:
                    ca=a.new_context();cb=b.new_context()
                    ca.add_cookies([{'name':'isolation','value':'a','url':'https://example.com'}])
                    assert not cb.cookies()
                    pa=ca.new_page();pb=cb.new_page()
                    pa.set_content('<title>A</title><h1>A</h1>');pb.set_content('<title>B</title><h1>B</h1>')
                    assert pa.title()=='A' and pb.title()=='B' and len(pa.screenshot())>100
                    a.close();assert pb.title()=='B'
                    print('REMOTE_BROWSER_ISOLATION_OK')
                finally:
                    if a.is_connected():a.close()
                    b.close()
    elif role=='bootstrap':
        admin()
        os.environ.update(APP_DB_PASSWORD=secret('db_app'),BACKUP_DB_PASSWORD=secret('db_backup'))
        execute('scripts/setup/bootstrap_server.py')
    elif role in {'backup','backup-loop'}:
        admin()
        if role=='backup':execute('scripts/backup/verify.py',['--directory','/backups'])
        else:
            import threading
            stop=threading.Event();child=None
            def shutdown(*_):
                stop.set()
                if child is not None and child.poll() is None:child.terminate()
            signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
            while not stop.is_set():
                child=subprocess.Popen([sys.executable,'scripts/backup/verify.py','--directory','/backups'])
                code=child.wait();child=None
                stop.wait(max(300,int(os.getenv('BACKUP_INTERVAL_SECONDS','86400'))) if code==0 else 300)
    else:raise SystemExit('Unsupported role')
if __name__=='__main__':main()

