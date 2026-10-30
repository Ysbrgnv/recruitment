"""Context-local transaction propagation. No connection is shared across threads."""
from contextvars import ContextVar
from contextlib import contextmanager
pg_connection=ContextVar('recruitment_pg_transaction',default=None)
worker_job=ContextVar('recruitment_worker_job',default=None)
@contextmanager
def bind(variable,value):
    token=variable.set(value)
    try:yield value
    finally:variable.reset(token)

@contextmanager
def submission_transaction():
    import os
    if os.getenv('TASK_QUEUE_BACKEND','local')!='postgres':
        yield
        return
    from app_state import database
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        yield

def atomic_submission(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args,**kwargs):
        with submission_transaction():return fn(*args,**kwargs)
    return wrapped

