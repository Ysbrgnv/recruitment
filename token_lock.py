"""Bounded OS file lock shared by processes using the same local token directory."""
import os,time,threading
from contextlib import contextmanager
_local=threading.local()
@contextmanager
def token_file_lock(env_path,timeout=35):
    path=str(env_path)+'.hh-auth.lock'
    held=getattr(_local,'held',{})
    if path in held:
        yield;return
    f=open(path,'a+b');locked=False
    try:
        if os.name=='nt':
            f.seek(0,2)
            if not f.tell():f.write(b'0');f.flush()
        deadline=time.monotonic()+timeout
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True;break
            except (BlockingIOError,OSError):
                if time.monotonic()>=deadline:raise TimeoutError('HH token lock timeout')
                time.sleep(.05)
        held[path]=True;_local.held=held
        yield
    finally:
        held.pop(path,None)
        if locked:
            if os.name=='nt':
                import msvcrt
                f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close()

