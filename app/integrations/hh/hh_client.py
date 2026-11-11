from __future__ import annotations
import logging, os, time, webbrowser, threading
from functools import wraps
from urllib.parse import urlencode
import requests
from dotenv import set_key, dotenv_values
from app.integrations.hh.token_lock import token_file_lock
from app.core.errors import HHUnavailableError
from app.persistence.app_state import APP_STATE, STATE_LOCK
from app.core.settings import *

logger = logging.getLogger(__name__)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": HH_USER_AGENT, "Accept": "application/json"})

AUTH_LOCK=threading.RLock()
_AUTH_GENERATION=0
_AUTH_LAST_ATTEMPT=None
_AUTH_LAST_TIME=0.0
_AUTH_LAST_RESULT=False

def synchronized_auth(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        with AUTH_LOCK:
            with token_file_lock(ENV_PATH):return fn(*args,**kwargs)
    return wrapped

@synchronized_auth
def save_tokens(access_token: str, refresh_token: str | None = None) -> None:
    global _AUTH_GENERATION
    # Persist both values with one atomic replacement, not two interleavable writes.
    import tempfile
    from pathlib import Path
    path=Path(ENV_PATH)
    fd,temp=tempfile.mkstemp(prefix='.hh-token-',dir=str(path.parent))
    os.close(fd)
    try:
        Path(temp).write_bytes(path.read_bytes() if path.exists() else b'')
        set_key(temp,"HH_BEARER",access_token)
        if refresh_token:set_key(temp,"HH_REFRESH_TOKEN",refresh_token)
        os.replace(temp,path)
        _refresh_state_path().unlink(missing_ok=True)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    os.environ["HH_BEARER"] = access_token
    _AUTH_GENERATION+=1
    SESSION.headers["Authorization"] = f"Bearer {access_token}"
    if refresh_token:
        os.environ["HH_REFRESH_TOKEN"] = refresh_token

def _auth_fingerprint():
    import hashlib,json
    values=[HH_CLIENT_ID,HH_CLIENT_SECRET,os.getenv('HH_REFRESH_TOKEN',''),os.getenv('HH_AUTH_REVISION','')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _refresh_state_path():
    from pathlib import Path
    return Path(ENV_PATH).with_name('.hh-refresh-state.json')


def _write_refresh_state(reason,blocked=True,retry_after=0):
    import json,tempfile
    path=_refresh_state_path()
    fd,temp=tempfile.mkstemp(prefix='.refresh-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump({'fingerprint':_auth_fingerprint(),'reason':reason,'blocked':blocked,'retry_after':retry_after},f)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def _auth_message(reason):
    hints={
        'invalid_client':'Проверьте HH_CLIENT_ID и HH_CLIENT_SECRET.',
        'account not found':'Проверьте пару HH_CLIENT_ID и HH_CLIENT_SECRET.',
        'token has already been refreshed':'Refresh-токен уже использован. Нужна актуальная пара токенов; не запускайте два хоста с одной парой.',
        'token was revoked':'Доступ отозван. Выполните повторную авторизацию HH.',
        'token not found':'Refresh-токен не найден. Выполните повторную авторизацию HH.',
        'token not expired':'HH сообщает, что access-токен ещё действует. Проверьте тип отказа и права доступа.',
        'refresh_outcome_unknown':'Исход обновления токена неизвестен. Повтор заблокирован: проверьте доступ и при необходимости авторизуйтесь заново.',
        'missing_credentials':'Заполните HH_CLIENT_ID, HH_CLIENT_SECRET и HH_REFRESH_TOKEN.',
    }
    return ('HH: '+hints.get(reason,'Обновление доступа отклонено; проверьте oauth_error в журнале и при необходимости авторизуйтесь заново.')+
            ' Обновите .env и запустите START_LOCAL.ps1. Обычный restart не импортирует .env.')


_AUTH_FAILURE_MESSAGE=''

@synchronized_auth
def refresh_access_token() -> bool:
    global _AUTH_FAILURE_MESSAGE
    import json
    reload_saved_tokens()
    refresh=os.getenv('HH_REFRESH_TOKEN','').strip()
    if not all([refresh,HH_CLIENT_ID,HH_CLIENT_SECRET]):
        _AUTH_FAILURE_MESSAGE=_auth_message('missing_credentials');return False
    path=_refresh_state_path()
    if path.exists():
        try:
            state=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,OSError):
            _AUTH_FAILURE_MESSAGE=_auth_message('refresh_outcome_unknown');return False
        if state.get('fingerprint')==_auth_fingerprint() and (state.get('blocked') or state.get('retry_after',0)>time.time()):
            _AUTH_FAILURE_MESSAGE=_auth_message(state.get('reason'));return False
    # A killed process or lost response may have consumed a single-use refresh token.
    # Persist intent BEFORE POST. No blind retry of a refresh with unknown outcome.
    _write_refresh_state('refresh_outcome_unknown')
    try:
        response=requests.post(HH_TOKEN_URL,data={'grant_type':'refresh_token','refresh_token':refresh,'client_id':HH_CLIENT_ID,'client_secret':HH_CLIENT_SECRET},headers={'User-Agent':HH_USER_AGENT},timeout=(10,REQUEST_TIMEOUT),allow_redirects=False)
        if response.status_code!=200:
            try:payload=response.json()
            except ValueError:payload={}
            if not isinstance(payload,dict):payload={}
            allowed={'invalid_grant','invalid_client','invalid_request','unauthorized_client','unsupported_grant_type','invalid_scope','forbidden'}
            descriptions={'account not found','account is locked','password invalidated','login not verified','bad redirect url','token is empty','token not found','token has already been refreshed','token not expired','token was revoked','bad token','token deactivated','app token refresh too early'}
            error=payload.get('error');description=payload.get('error_description')
            error=error if isinstance(error,str) and error in allowed else 'unclassified'
            description=description if isinstance(description,str) and description in descriptions else 'unclassified'
            reason=description if description!='unclassified' else error
            permanent=response.status_code!=429 and description not in {'token not expired','app token refresh too early'}
            _write_refresh_state(reason,blocked=permanent,retry_after=time.time()+300)
            _AUTH_FAILURE_MESSAGE=_auth_message(reason)
            logger.warning('HH token refresh HTTP %s (oauth_error=%s, reason=%s)',response.status_code,error,description)
            return False
        data=response.json()
        access=data.get('access_token') if isinstance(data,dict) else None
        rotated=data.get('refresh_token') if isinstance(data,dict) else None
        if not isinstance(access,str) or not access.strip() or (rotated is not None and (not isinstance(rotated,str) or not rotated.strip())):
            _AUTH_FAILURE_MESSAGE=_auth_message('refresh_outcome_unknown')
            logger.warning('HH refresh returned an invalid token payload; repeat blocked');return False
        save_tokens(access,rotated or refresh)
        _AUTH_FAILURE_MESSAGE=''
        logger.info('HH token refresh succeeded; token pair saved atomically')
        return True
    except requests.exceptions.ConnectTimeout:
        _write_refresh_state('connection_timeout',blocked=False,retry_after=time.time()+30)
        _AUTH_FAILURE_MESSAGE='HH: соединение для обновления доступа не установлено. Повторите позже.'
        logger.warning('HH refresh connect timeout; cooldown enabled');return False
    except Exception as exc:
        _AUTH_FAILURE_MESSAGE=_auth_message('refresh_outcome_unknown')
        logger.warning('HH refresh failed (%s); outcome unknown, repeat blocked',type(exc).__name__)
        return False

@synchronized_auth
def authorize_from_scratch() -> bool:
    if os.getenv('HH_INTERACTIVE_AUTH','false').lower()!='true':
        raise HHUnavailableError('Интерактивная авторизация HH отключена. Настройте токены заранее.')
    if not all([HH_CLIENT_ID, HH_CLIENT_SECRET, HH_REDIRECT_URI]): return False
    webbrowser.open(HH_AUTHORIZE_URL + "?" + urlencode({"response_type":"code", "client_id":HH_CLIENT_ID, "redirect_uri":HH_REDIRECT_URI}))
    code = input("Введите code из адресной строки HH: ").strip()
    if not code: return False
    try:
        response = requests.post(HH_TOKEN_URL, data={"grant_type":"authorization_code", "code":code, "client_id":HH_CLIENT_ID, "client_secret":HH_CLIENT_SECRET, "redirect_uri":HH_REDIRECT_URI}, timeout=(10, REQUEST_TIMEOUT))
        response.raise_for_status(); data = response.json(); save_tokens(data["access_token"], data.get("refresh_token")); return True
    except Exception:
        logger.exception("HH authorization failed"); return False

@synchronized_auth
def ensure_token() -> str | None:
    token = os.getenv("HH_BEARER", "").strip()
    if token: SESSION.headers["Authorization"] = f"Bearer {token}"; return token
    return os.getenv("HH_BEARER") if refresh_access_token() or authorize_from_scratch() else None

def reload_saved_tokens():
    global _AUTH_GENERATION
    values=dotenv_values(ENV_PATH,interpolate=False)
    token=values.get('HH_BEARER')
    if token and token!=os.getenv('HH_BEARER',''):
        os.environ['HH_BEARER']=token;_AUTH_GENERATION+=1
    if values.get('HH_REFRESH_TOKEN'):os.environ['HH_REFRESH_TOKEN']=values['HH_REFRESH_TOKEN']

@synchronized_auth
def recover_auth(rejected_token,generation):
    """Only the first rejected request refreshes; late responses reuse new credentials."""
    global _AUTH_LAST_ATTEMPT,_AUTH_LAST_TIME,_AUTH_LAST_RESULT
    with AUTH_LOCK:
        reload_saved_tokens()
        current=os.getenv('HH_BEARER','').strip()
        if current and current!=rejected_token:return True
        key=(rejected_token,os.getenv('HH_REFRESH_TOKEN',''))
        if _AUTH_LAST_ATTEMPT==key and time.monotonic()-_AUTH_LAST_TIME<5:
            return _AUTH_LAST_RESULT
        _AUTH_LAST_ATTEMPT=key
        _AUTH_LAST_RESULT=False
        try:_AUTH_LAST_RESULT=bool(refresh_access_token())
        finally:_AUTH_LAST_TIME=time.monotonic()
        return _AUTH_LAST_RESULT

def hh_get(endpoint: str, params: dict | None = None, reauth: bool = True, retries: int = 3):
    url = endpoint if endpoint.startswith("http") else f"{HH_API_BASE}/{endpoint.lstrip('/')}"
    with AUTH_LOCK:
        with token_file_lock(ENV_PATH):reload_saved_tokens()
        token=os.getenv('HH_BEARER','').strip()
        generation=_AUTH_GENERATION
    # Do not mutate a shared requests.Session while other threads are sending.
    headers={"User-Agent":HH_USER_AGENT,"Accept":"application/json"}
    if token:headers['Authorization']='Bearer '+token
    try:
        response = requests.get(url, params=params, headers=headers, timeout=(10, REQUEST_TIMEOUT))
    except (requests.ConnectionError, requests.Timeout) as exc:
        if retries <= 0:
            raise HHUnavailableError("HH временно недоступен. Повторите попытку позже.") from exc
        logger.warning("Сетевая ошибка HH (%s), повтор через 2с, осталось %s", exc, retries)
        time.sleep(2)
        return hh_get(endpoint, params, reauth, retries - 1)
    if response.status_code == 429 and retries > 0:
        try: wait = max(1, int(response.headers.get("Retry-After", "5")))
        except ValueError: wait = 5
        time.sleep(wait); return hh_get(endpoint, params, reauth, retries - 1)
    if response.status_code in (401,403):
        try:payload=response.json()
        except ValueError:payload={}
        entries=payload.get('errors',[]) if isinstance(payload,dict) else []
        if not isinstance(entries,list):entries=[]
        oauth={e.get('value') for e in entries if isinstance(e,dict) and e.get('type')=='oauth' and isinstance(e.get('value'),str)}
        if oauth & {'token_revoked','application_not_found'}:
            raise HHUnavailableError('HH: доступ отозван или приложение удалено. Требуется новая авторизация; внесите новую пару токенов в .env и запустите START_LOCAL.ps1.')
        if reauth and (response.status_code==401 or 'token_expired' in oauth):
            if recover_auth(token,generation):return hh_get(endpoint,params,False,retries)
            raise HHUnavailableError(_AUTH_FAILURE_MESSAGE or _auth_message('unclassified'))
        if response.status_code==403:
            raise HHUnavailableError('HH вернул HTTP 403: проверьте права аккаунта, оплаченный доступ к методу и тип авторизации. Refresh не выполнялся: истечение токена не подтверждено.')
        raise HHUnavailableError('HH отклонил обновлённый access-токен. Проверьте авторизацию; повторный refresh в этом запросе заблокирован.')
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HHUnavailableError(f"HH вернул HTTP {response.status_code}.") from exc
    return response

def fetch_active_vacancies(employer_id: str) -> list[dict]:
    result, page = [], 0
    while True:
        logger.info("[STARTUP] Загрузка вакансий HH: страница %s", page + 1)
        data = hh_get("/vacancies", {"employer_id": employer_id, "per_page": 100, "page": page}).json()
        result.extend(data.get("items", [])); page += 1
        if page >= data.get("pages", 0):
            logger.info("[STARTUP] Вакансии загружены: %s", len(result)); return result

def fetch_vacancy_details(vacancy_id: str) -> dict:
    if not vacancy_id.isdigit(): raise ValueError("Некорректный ID вакансии")
    with STATE_LOCK: cached = APP_STATE["vacancy_cache"].get(vacancy_id)
    if cached: return cached
    data = hh_get(f"/vacancies/{vacancy_id}").json()
    with STATE_LOCK: APP_STATE["vacancy_cache"][vacancy_id] = data
    return data

def search_resumes(params: dict) -> dict: return hh_get("/resumes", params).json()
def fetch_resume_details(resume_id: str) -> dict: return hh_get(f"/resumes/{resume_id}").json()

