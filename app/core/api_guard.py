"""Browser-session task ownership. Not employee SSO."""
import hashlib,re,secrets
from http.cookies import SimpleCookie
class RequestError(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status

def identity(headers):
    try:
        cookies=SimpleCookie();cookies.load(headers.get('Cookie',''));value=cookies['task_session'].value
        if re.fullmatch('[a-f0-9]{64}',value):return hashlib.sha256(value.encode()).hexdigest()
    except (KeyError,ValueError):pass
    return None

def owns(task,owner):
    return bool(task and owner and task.get('owner_session') and secrets.compare_digest(task['owner_session'],owner))

