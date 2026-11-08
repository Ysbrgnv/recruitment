"""Explicit administrator-only schema upgrade and least-privilege grants."""
import os,subprocess,sys
from pathlib import Path
import psycopg
from psycopg import sql
from dotenv import load_dotenv

def main():
    root=Path(__file__).resolve().parent
    load_dotenv(root/'.env.infrastructure')
    admin=os.environ['PG_ADMIN_URL'];app_password=os.environ['APP_DB_PASSWORD'];backup_password=os.environ['BACKUP_DB_PASSWORD']
    env=dict(os.environ,DATABASE_URL=admin,APP_SCHEMA_MANAGED='false',TASK_QUEUE_BACKEND='postgres')
    # Use a fresh process so imported modules cannot retain runtime credentials.
    subprocess.run([sys.executable,'-c',
        'import app_state,operation_store,durable_queue,shared_screen; from app.core import telemetry; app_state.init_db(); operation_store.init(); durable_queue.init(); shared_screen.init(); telemetry.init()'],
        cwd=root,env=env,check=True,timeout=60)
    with psycopg.connect(admin) as c:
        db=c.execute('SELECT current_database()').fetchone()[0]
        for name,password in [('recruitment_app',app_password),('recruitment_backup',backup_password)]:
            if not c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():
                c.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(name)))
            c.execute(sql.SQL('ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
            c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db),sql.Identifier(name)))
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(name)))
        c.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        c.execute(sql.SQL('REVOKE CREATE,TEMPORARY ON DATABASE {} FROM PUBLIC').format(sql.Identifier(db)))
        c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO recruitment_app')
        c.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO recruitment_app')
        c.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO recruitment_backup')
        c.execute('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO recruitment_backup')
        c.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO recruitment_app')
        c.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO recruitment_app')
        c.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO recruitment_backup')
        c.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO recruitment_backup')
    print('Schema upgraded; runtime DML-only role and read-only backup role configured.')
if __name__=='__main__':main()

