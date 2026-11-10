"""Local acceptance probes. Does not submit candidates, AI requests or email."""
import argparse,sys,json,hashlib,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docker.container_entry import runtime
runtime()
import psycopg
from psycopg import sql

def snapshot():
    result={}
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        active=c.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','failed','interrupted','waiting_manual','waiting_query')").fetchone()[0]
        if active:raise RuntimeError('Active tasks exist. Finish them before acceptance.')
        for table in ['history','operations']:
            digest=hashlib.sha256();count=0
            for (value,) in c.execute(sql.SQL('SELECT md5(row_to_json(t)::text) AS digest FROM {} t ORDER BY digest').format(sql.Identifier(table))):
                digest.update(value.encode());digest.update(b'\n');count+=1
            result[table]={'rows':count,'sha256':digest.hexdigest()}
        result['succeeded_ai']=c.execute("SELECT COUNT(*) FROM operations WHERE kind='ai' AND state='succeeded'").fetchone()[0]
        result['succeeded_mail']=c.execute("SELECT COUNT(*) FROM operations WHERE kind='hh_email' AND state='succeeded'").fetchone()[0]
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['snapshot','compare','hh']);a=p.parse_args()
    if a.action=='hh':
        from app.integrations.hh.hh_client import hh_get
        hh_get('/me');print('HH_CURRENT_ACCESS_OK (expiry refresh not forced)');return
    value=snapshot()
    if a.action=='snapshot':print(json.dumps(value));return
    before=json.load(sys.stdin)
    if value!=before:raise RuntimeError('History or operation journal changed across infrastructure restart')
    print('RESTART_PERSISTENCE_VERIFIED')
    if not value['succeeded_ai'] or not value['succeeded_mail']:
        print('LIVE_CANDIDATE_CYCLE_NOT_VERIFIED: complete one candidate and check the received email, then rerun.')
    else:print('SAVED_SUCCESS_RECORDS_PRESENT: delivery and absence of provider-side duplicates still require live acceptance.')
if __name__=='__main__':main()

