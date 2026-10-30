"""Retry only classified transient failures; unknown external effects always pause."""
import os,random

def transient(exc):
    import psycopg,requests
    from psycopg_pool import PoolTimeout
    from errors import AIUnavailableError,MailUnavailableError
    # AI/mail wrappers may describe a sent request without a complete response.
    # Never infer that such an operation is safe merely from its network cause.
    seen=set();current=exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current,(AIUnavailableError,MailUnavailableError)):return False
        current=current.__cause__
    current=exc;seen.clear()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current,(psycopg.OperationalError,psycopg.errors.SerializationFailure,
            psycopg.errors.DeadlockDetected,psycopg.errors.LockNotAvailable,
            psycopg.errors.QueryCanceled,PoolTimeout,requests.ConnectionError,requests.Timeout)):return True
        if isinstance(current,requests.HTTPError):
            return current.response is not None and current.response.status_code in (429,502,503,504)
        current=current.__cause__
    return False

def maximum():return max(1,min(10,int(os.getenv('JOB_MAX_ATTEMPTS','3'))))

def delay(attempt):
    base=max(1,float(os.getenv('JOB_RETRY_BASE_SECONDS','5')))
    cap=max(base,float(os.getenv('JOB_RETRY_MAX_SECONDS','300')))
    return min(cap,base*2**max(0,attempt-1)*random.uniform(.8,1.2))

