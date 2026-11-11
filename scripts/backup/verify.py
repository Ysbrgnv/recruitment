"""Consistent pg_dump + actual restore into a disposable DB + row-count verification.
No restore is ever performed over the live database. Admin credentials are separate.
"""
import argparse,hashlib,json,os,shutil,subprocess,time,uuid,signal
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from dotenv import load_dotenv

def command(binary,url,args):
    parts=conninfo_to_dict(url);env=dict(os.environ)
    password=parts.pop('password',None)
    if password:env['PGPASSWORD']=password
    else:env.pop('PGPASSWORD',None)
    # Passwords never appear in argv or captured output.
    process=subprocess.Popen([binary,'--dbname',make_conninfo(**parts),*args],env=env,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:out,err=process.communicate(timeout=600)
    except BaseException:
        process.terminate()
        try:process.communicate(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.communicate()
        raise
    if process.returncode:raise RuntimeError(Path(binary).name+' failed (details withheld; check database/tool versions)')

def counts(con):
    tables=con.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    return {row[0]:con.execute(sql.SQL('SELECT COUNT(*) FROM public.{}').format(sql.Identifier(row[0]))).fetchone()[0] for row in tables}

def content_fingerprints(con):
    """Order-independent, streaming fingerprints; never log row contents."""
    result={}
    tables=con.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    for (table,) in tables:
        digest=hashlib.sha256();count=0
        with con.cursor(name='verify_'+uuid.uuid4().hex) as cursor:
            cursor.execute(sql.SQL("SELECT md5(row_to_json(t)::text) AS digest FROM public.{} t ORDER BY digest").format(sql.Identifier(table)))
            for (value,) in cursor:
                digest.update(value.encode('ascii'));digest.update(b'\n');count+=1
        result[table]={'rows':count,'sha256_of_sorted_row_md5':digest.hexdigest()}
    return result


def run_backup(admin,reader,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    try:directory.chmod(0o700)
    except OSError:pass
    pg_dump=os.getenv('PG_DUMP_BIN') or shutil.which('pg_dump')
    pg_restore=os.getenv('PG_RESTORE_BIN') or shutil.which('pg_restore')
    if not pg_dump or not pg_restore:raise RuntimeError('Install PostgreSQL client tools and set PG_DUMP_BIN / PG_RESTORE_BIN')
    stem=time.strftime('recruitment-%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    partial=directory/(stem+'.partial');final=directory/(stem+'.dump')
    verify='verify_'+uuid.uuid4().hex
    target=make_conninfo(admin,dbname=verify)
    created=False
    try:
        with psycopg.connect(reader) as snapshot:
            snapshot.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            token=snapshot.execute('SELECT pg_export_snapshot()').fetchone()[0]
            expected=counts(snapshot)
            expected_content=content_fingerprints(snapshot)
            command(pg_dump,reader,['--format=custom','--no-owner','--no-privileges','--snapshot='+token,'--file',str(partial)])
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(verify)));created=True
        command(pg_restore,target,['--exit-on-error','--no-owner','--no-privileges',str(partial)])
        with psycopg.connect(target) as c:
            actual=counts(c)
            actual_content=content_fingerprints(c)
        if actual!=expected:raise RuntimeError('Restored row counts differ from the exported snapshot')
        if actual_content!=expected_content:raise RuntimeError('Restored table contents differ from exported snapshot')
        with partial.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        partial.replace(final)
        try:final.chmod(0o600)
        except OSError:pass
        manifest={'file':final.name,'sha256':digest,'verified_at':time.time(),'tables':expected,
            'restore_verified':True,'content_verified':True,'content_fingerprints':expected_content,'method':'pg_dump exported snapshot; disposable pg_restore; row counts and sorted row content fingerprints'}
        (directory/(stem+'.json')).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        return manifest
    finally:
        if created:
            with psycopg.connect(admin,autocommit=True) as c:
                c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(verify)))
        if partial.exists():partial.unlink()

def prune_verified(directory,days=14):
    manifests=[]
    for path in Path(directory).glob('recruitment-*.json'):
        try:
            item=json.loads(path.read_text(encoding='utf-8'))
            name=item['file']
            if not item.get('restore_verified') or Path(name).name!=name or not name.startswith('recruitment-') or not name.endswith('.dump'):continue
            manifests.append((item['verified_at'],path,Path(directory)/name))
        except (ValueError,KeyError,OSError):continue
    manifests.sort(reverse=True)
    for stamp,path,dump in manifests[2:]:
        if stamp<time.time()-max(1,days)*86400:dump.unlink(missing_ok=True);path.unlink(missing_ok=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',default='backups');args=parser.parse_args()
    def stop(*_):raise KeyboardInterrupt('Backup stopped')
    for sig in [signal.SIGINT,signal.SIGTERM]+([signal.SIGBREAK] if os.name=='nt' else []):signal.signal(sig,stop)
    root=Path(__file__).resolve().parents[2];load_dotenv(root/'.env.infrastructure')
    admin=os.environ['PG_ADMIN_URL'];reader=os.environ['PG_BACKUP_URL']
    directory=Path(args.directory)
    if not directory.is_absolute():directory=root/directory
    try:
        manifest=run_backup(admin,reader,directory)
        # Backup process has separate credentials; only counters are updated here.
        with psycopg.connect(admin) as c:
            for name,value in [('backup_last_verified_timestamp',manifest['verified_at']),('backup_last_run_ok',1)]:
                c.execute('INSERT INTO service_counters VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value',(name,value))
        prune_verified(directory,int(os.getenv('BACKUP_KEEP_DAYS','14')))
        print('BACKUP_RESTORE_VERIFIED',manifest['file'])
    except Exception:
        try:
            with psycopg.connect(admin) as c:
                c.execute("INSERT INTO service_counters VALUES('backup_last_run_ok',0) ON CONFLICT(name) DO UPDATE SET value=0")
        except Exception:pass
        raise
if __name__=='__main__':main()

