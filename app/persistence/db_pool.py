"""Bounded, lazy per-process pools. Dedicated fencing connections are NOT pooled."""
import os,threading,atexit
from contextlib import contextmanager
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
_lock=threading.Lock()
_pools={}

def connection_kwargs():
    return dict(row_factory=dict_row,connect_timeout=5,
        options='-c statement_timeout='+os.getenv('DB_STATEMENT_TIMEOUT_MS','10000')+
                ' -c lock_timeout='+os.getenv('DB_LOCK_TIMEOUT_MS','3000')+
                ' -c idle_in_transaction_session_timeout=15000',
        keepalives=1,keepalives_idle=10,keepalives_interval=5,keepalives_count=3,
        tcp_user_timeout=15000,application_name='recruitment-'+str(os.getpid()))

def get_pool(url=None):
    url=url or os.environ['DATABASE_URL'];key=(os.getpid(),url)
    with _lock:
        if key not in _pools:
            from psycopg.conninfo import conninfo_to_dict
            kwargs=connection_kwargs()
            kwargs["options"]=conninfo_to_dict(url).get("options", "")+" "+kwargs["options"]
            pool=ConnectionPool(url,min_size=0,max_size=int(os.getenv('DB_POOL_MAX','6')),
                max_waiting=32,timeout=5,max_idle=60,max_lifetime=1800,
                kwargs=kwargs,check=ConnectionPool.check_connection,open=True)
            _pools[key]=pool
        return _pools[key]

@contextmanager
def connection(url=None):
    with get_pool(url).connection() as con:
        yield con

def close_pools():
    with _lock:
        pools=list(_pools.values());_pools.clear()
    for pool in pools:pool.close(timeout=5)
atexit.register(close_pools)

