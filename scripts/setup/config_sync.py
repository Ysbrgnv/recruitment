"""Pure configuration merge; preserve runtime token rotation unless source auth changes."""
import hashlib,json,os,tempfile
from pathlib import Path
from dotenv import dotenv_values,set_key
AUTH_KEYS=('HH_CLIENT_ID','HH_CLIENT_SECRET','HH_BEARER','HH_REFRESH_TOKEN','HH_AUTH_REVISION')
ALLOWED={'EMPLOYER_ID','API_NANO_URL','MODEL_NANO','NANO_GPT_API_KEY','NANO_TIMEOUT','RESUMES_TO_FIND','MAX_BACKGROUND_WORKERS','TASK_RETENTION_HOURS','RESERVATION_STALE_MINUTES','GMAIL_USER','GMAIL_SMTP_PORT','GMAIL_TIMEOUT_SECONDS','CAPTCHA_WAIT_SECONDS','STUCK_AFTER_SECONDS','JOB_HARD_TIMEOUT_SECONDS','WATCHDOG_POLL_SECONDS'}
def allowed(k):
    return k in ALLOWED or k.startswith(('HH_','RESUME_','STAGE_'))
def atomic_env(path,values,mode=0o600):
    path=Path(path);fd,temp=tempfile.mkstemp(prefix='.config-',dir=str(path.parent));os.close(fd)
    try:
        for k,v in values.items():
            if v is not None:set_key(temp,k,str(v))
        os.chmod(temp,mode);os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
def sync(seed,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'hh.env';meta=folder/'.source-auth.json'
    incoming={k:v for k,v in dotenv_values(seed,interpolate=False).items() if v is not None and allowed(k)}
    fingerprint=hashlib.sha256(json.dumps([incoming.get(k,'') for k in AUTH_KEYS]).encode()).hexdigest()
    old=dotenv_values(path,interpolate=False) if path.is_file() else {}
    previous=json.loads(meta.read_text()) if meta.is_file() else {}
    auth_changed=not path.exists() or previous.get('fingerprint')!=fingerprint
    merged=dict(incoming)
    if not auth_changed:
        for k in ('HH_BEARER','HH_REFRESH_TOKEN'):
            if old.get(k):merged[k]=old[k]
    merged['HH_INTERACTIVE_AUTH']='false'
    atomic_env(path,merged)
    if auth_changed:(folder/'.hh-refresh-state.json').unlink(missing_ok=True)
    # Save source fingerprint AFTER hh.env. If interrupted, the next sync reapplies safely.
    fd,temp=tempfile.mkstemp(prefix='.source-',dir=str(folder))
    try:
        with os.fdopen(fd,'w') as f:json.dump({'fingerprint':fingerprint},f)
        os.replace(temp,meta)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return auth_changed

