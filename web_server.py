from __future__ import annotations

import json
import secrets
import os
from api_guard import RequestError,identity,owns
import logging
import mimetypes
import re
import socket
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from app_state import APP_STATE, EXECUTOR, STATE_LOCK, create_task, get_task, update_task, set_review, save_query, claim_review, cancel_review, recover_tasks, expire_reviews, list_tasks
from errors import UserFacingError
from hh_client import fetch_active_vacancies, fetch_vacancy_details
from recruitment_data import clean_html
from recruitment_service import find_and_evaluate, prepare_query, finish_xray_query
from security import sanitize_vacancy_html, valid_email
import operation_store as operations
from workflow import run_hh, run_xray, NeedsManual
from browser_pause import request_action
from xray_plan import validate_sources, validate_plan, DEFAULT_SOURCES
from tracking_service import import_legacy_history
from settings import (
    APP_HOST, APP_PORT, BASE_DIR, DEFAULT_EVALUATION_COUNT, EMPLOYER_ID,
    TASK_RETENTION_HOURS,
)

logger = logging.getLogger(__name__)
UI_DIR = BASE_DIR / "ui"


def enum_name(value):
    return (value or {}).get("name") or "Не указано"


def salary_text(salary):
    if not salary:
        return "Не указана"
    parts = []
    if salary.get("from") is not None:
        parts.append(f"от {salary['from']:,}".replace(",", " "))
    if salary.get("to") is not None:
        parts.append(f"до {salary['to']:,}".replace(",", " "))
    return " ".join(parts) + (" " + str(salary.get("currency")) if salary.get("currency") else "")


def vacancy_payload(vacancy):
    return {
        "id": vacancy.get("id"), "name": vacancy.get("name"),
        "city": (vacancy.get("area") or {}).get("name"),
        "experience": enum_name(vacancy.get("experience")),
        "employment": enum_name(vacancy.get("employment")),
        "schedule": enum_name(vacancy.get("schedule")),
        "salary": salary_text(vacancy.get("salary")),
        "skills": [x.get("name") for x in vacancy.get("key_skills") or [] if x.get("name")],
        "description": sanitize_vacancy_html(vacancy.get("description")),
        "url": vacancy.get("alternate_url"), "default_count": DEFAULT_EVALUATION_COUNT,
    }


def load_vacancies():
    if not STATE_LOCK.acquire(blocking=False):
        logger.info("Загрузка вакансий уже выполняется; повторный запрос пропущен")
        return False
    try:
        print("[STARTUP] Загрузка вакансий...", flush=True)
        try:
            fresh = fetch_active_vacancies(EMPLOYER_ID)
        except UserFacingError as exc:
            logger.warning("Вакансии HH недоступны: %s", exc)
            with STATE_LOCK:APP_STATE["startup_error"] = str(exc)
            return False
        except Exception:
            logger.exception("Не удалось загрузить вакансии")
            with STATE_LOCK:APP_STATE["startup_error"] = "Не удалось загрузить вакансии из HH."
            return False
        with STATE_LOCK:
            APP_STATE["vacancies"] = fresh
            APP_STATE["vacancy_cache"] = {}
            APP_STATE["startup_error"] = None
        print(f"[STARTUP] Вакансии загружены: {len(fresh)}", flush=True)
        return True
    finally:
        STATE_LOCK.release()


def cleanup_tasks():
    expire_reviews()


def run_task(task_id, prepare=False, cooperative=False, hh_steps=None):
    try:
        task = get_task(task_id)
        update_task(task_id,error_retryable=False)
        def progress(stage, message, details=None):
            logger.info("[TASK %s] %s", task_id[:8], message)
            update_task(task_id, status="running", stage=stage, message=message, progress=details or {})
        if hh_steps is None:
            progress("query" if prepare else "setup", "Формируется запрос" if prepare else "Подготавливается поиск")
        if prepare:
            prepare_query(task["vacancy_id"], task_id, task["mode"])
            return  # Approval is persisted; no worker waits for a click.
        if task["mode"] == "xray":
            result = run_xray(task_id, progress)
        else:
            if cooperative:
                from workflow import run_hh_steps
                hh_steps=hh_steps if hh_steps is not None else run_hh_steps(task_id,progress,staged=True)
                try:
                    next_stage=next(hh_steps)
                    return (hh_steps,next_stage)
                except StopIteration as done:result=done.value
            else:result = run_hh(task_id, progress)
        update_task(task_id, status="completed", stage="complete", message="Задача завершена", progress={**result.get("metrics", {}), "requested_count": task["requested_count"], "target_pool": result.get("target_pool", 0)}, result=result, finished=time.time())
    except NeedsManual:
        return
    except Exception as exc:
        logger.exception("Задача %s завершилась ошибкой", task_id)
        operations.mark_inflight_unknown(task_id)
        if operations.unknowns(task_id):
            update_task(task_id,status='waiting_manual',stage='manual_resolution',message='Исход операции неизвестен. Требуется ручная проверка.')
            return
        if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
            from retry_policy import transient
            update_task(task_id,error_retryable=transient(exc))
        update_task(task_id, status="failed", stage="failed", message="Ошибка", error=str(exc) if isinstance(exc, UserFacingError) else "Не удалось выполнить задачу. Подробности в logs/*.jsonl.", finished=time.time())


def submit_task(task_id,prepare=False):
    if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        from durable_queue import enqueue
        return enqueue(task_id,prepare)
    task=get_task(task_id)
    if task and task.get('mode')=='hh':
        from hh_scheduler import HH_SCHEDULER
        return HH_SCHEDULER.submit(run_task,task_id,prepare)
    return EXECUTOR.submit(run_task,task_id,prepare)

from transaction_scope import atomic_submission
@atomic_submission
def start_task(vacancy_id, requested_count, responsible_email, mode="hh", xray_sources=None, owner_session=None):
    task_id, created = create_task(uuid.uuid4().hex, vacancy_id, requested_count, responsible_email, mode)
    if created:
        update_task(task_id,owner_session=owner_session)
        if mode=='xray':update_task(task_id,xray_sources=xray_sources or DEFAULT_SOURCES)
        submit_task(task_id, True)
    return task_id, created


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/tasks/"):
            logger.info(fmt, *args)

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path):
        if not path.is_file() or UI_DIR not in path.resolve().parents:
            return self.send_json({"error": "Not found"}, 404)
        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        return self._request_json

    def _parse_json(self):
        if self.headers.get('Transfer-Encoding'):raise RequestError('Transfer-Encoding не поддерживается')
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)>1 or (lengths and not lengths[0].isdigit()):raise RequestError('Неверный Content-Length')
        if lengths and len(lengths[0])>10:raise RequestError('Неверный Content-Length')
        length=int(lengths[0]) if lengths else 0
        if length==0:return {}
        if length>65536:raise RequestError('Размер запроса превышает 64 КиБ',413)
        if self.headers.get_content_type()!='application/json':raise RequestError('Требуется application/json',415)
        self.connection.settimeout(10)
        try:raw=self.rfile.read(length)
        except (TimeoutError,OSError):raise RequestError('Истекло время чтения запроса',408)
        if len(raw)!=length:raise RequestError('Неполное тело запроса')
        try:
            def pairs(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError('Повторяющееся поле')
                    result[key]=value
                return result
            def constant(value):raise ValueError('Недопустимое число')
            data=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
        except (ValueError,UnicodeError,RecursionError):raise RequestError('Некорректный JSON')
        if not isinstance(data,dict):raise RequestError('JSON должен быть объектом')
        return data

    def _task_access(self,path):
        match=re.match(r'/api/tasks/([0-9a-f]{32})(?:/|$)',path)
        if match and not owns(get_task(match.group(1)),identity(self.headers)):
            self.send_json({'error':'Задача недоступна для этой сессии.'},403);return False
        return True

    def _screen_stream(self, task_id):
        import browser_screen
        token = parse_qs(urlparse(self.path).query).get('token', [''])[0]
        if not browser_screen.open_stream(task_id, token):
            return self.send_json({'error':'Проверка завершена или лимит подключений исчерпан.'}, 409)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=screenframe')
            self.send_header('Cache-Control', 'no-store, private')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            seq = -1
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not owns(get_task(task_id), identity(self.headers)):
                    break
                item = browser_screen.next_frame(task_id, token, seq)
                if item is None:
                    break
                if not item['frame']:
                    continue
                seq = item['seq']
                data = item['frame']
                self.wfile.write(b'--screenframe\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(data)).encode('ascii') + b'\r\n\r\n' + data + b'\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            browser_screen.close_stream(task_id, token)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._task_access(path):return
        stream_match=re.fullmatch(r'/api/tasks/([0-9a-f]{32})/screen/stream',path)
        if stream_match:return self._screen_stream(stream_match.group(1))
        screen_match=re.fullmatch(r'/api/tasks/([0-9a-f]{32})/screen(?:/(frame))?',path)
        if screen_match:
            import browser_screen,base64
            task_id=screen_match.group(1)
            if screen_match.group(2):
                snapshot=browser_screen.frame(task_id)
                if not snapshot:return self.send_json({'error':'Проверка завершена или не ожидается.'},409)
                image=snapshot.pop('frame')
                snapshot['image']=base64.b64encode(image).decode('ascii') if image and parse_qs(urlparse(self.path).query).get('meta')!=['1'] else None
                return self.send_json(snapshot)
            return self.send_file(UI_DIR/'static'/'browser_screen.html')
        if path=='/' and not identity(self.headers):
            raw=(UI_DIR/'index.html').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Set-Cookie','task_session='+secrets.token_hex(32)+'; Path=/; HttpOnly; SameSite=Strict'+('; Secure' if os.getenv('SESSION_COOKIE_SECURE','false').lower()=='true' else ''))
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw);return
        if path == "/":
            return self.send_file(UI_DIR / "index.html")
        if path.startswith("/static/"):
            return self.send_file(UI_DIR / path.lstrip("/"))
        if path == "/api/vacancies":
            with STATE_LOCK:
                error = APP_STATE["startup_error"]
                items = [{"id": x.get("id"), "name": x.get("name"), "city": (x.get("area") or {}).get("name")} for x in APP_STATE["vacancies"]]
            return self.send_json({"error": error}, 500) if error else self.send_json(items)
        match = re.fullmatch(r"/api/vacancy/(\d+)", path)
        if match:
            try:
                return self.send_json(vacancy_payload(fetch_vacancy_details(match.group(1))))
            except UserFacingError as exc:
                logger.warning("Вакансия HH недоступна: %s", exc)
                return self.send_json({"error": str(exc)}, 503)
            except Exception:
                logger.exception("Ошибка вакансии")
                return self.send_json({"error": "Не удалось загрузить вакансию."}, 500)
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})", path)
        if match:
            task = get_task(match.group(1))
            payload = {key: task.get(key) for key in ("status", "stage", "message", "progress", "result", "error", "mode", "expires")} if task else None
            if payload:
                payload['unknown_operations']=[{k: op[k] for k in ('id','kind','item_key','state')} for op in operations.unknowns(match.group(1))]
            return self.send_json(payload or {"error": "Задача не найдена"}, 200 if payload else 404)
        match = re.fullmatch(r"/api/vacancy/(\d+)/tasks", path)
        if match:
            return self.send_json([{k: t.get(k) for k in ("task_id", "status", "mode", "message")} for t in list_tasks(match.group(1),owner_session=identity(self.headers)) if owns(t,identity(self.headers))])
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not identity(self.headers):return self.send_json({'error':'Откройте главную страницу для создания сессии.'},403)
        if not self._task_access(path):return
        try:self._request_json=self._parse_json()
        except RequestError as exc:
            self.close_connection=True
            return self.send_json({'error':str(exc)},exc.status)
        return self._post_validated(path)

    def _post_validated(self,path):
        screen_match=re.fullmatch(r'/api/tasks/([0-9a-f]{32})/screen/input',path)
        if screen_match:
            import browser_screen
            try:
                ok=browser_screen.enqueue(screen_match.group(1),self._request_json)
                return self.send_json({'ok':True} if ok else {'error':'Эта проверка уже завершена.'},200 if ok else 409)
            except ValueError as exc:return self.send_json({'error':str(exc)},400)
        if path == "/api/refresh":
            if not load_vacancies():
                return self.send_json({"error": (APP_STATE.get("startup_error") or "Не удалось обновить вакансии из HH.")+" Предыдущий список сохранён."}, 502)
            return self.send_json({"ok": True})
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/browser-(open|continue|cancel)", path)
        if match:
            if 'screen_token' in self._request_json:
                import browser_screen
                current=browser_screen.frame(match.group(1))
                if not current or current['token']!=self._request_json['screen_token']:
                    return self.send_json({'error':'Эта проверка уже завершена.'},409)
            ok=request_action(match.group(1),match.group(2))
            return self.send_json({'ok':True} if ok else {'error':'Проверка не ожидается или команда уже принята.'},200 if ok else 409)
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/resolve", path)
        if match:
            data=self.read_json()
            try:operations.resolve(match.group(1),str(data.get('operation_id') or ''),str(data.get('action') or ''),str(data.get('reason') or ''),actor_session=identity(self.headers))
            except ValueError as exc:return self.send_json({'error':str(exc)},409)
            return self.send_json({'ok':True})
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/resume", path)
        if match:
            tid=match.group(1)
            if operations.unknowns(tid):return self.send_json({'error':'Сначала разрешите операции с неизвестным исходом.'},409)
            from app_state import database, _read, _save
            with database() as con:
                con.execute('BEGIN IMMEDIATE')
                task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
                if not task or task['status'] not in {'waiting_manual','failed','interrupted'}:return self.send_json({'error':'Возобновление недоступно.'},409)
                if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
                    if con.execute("SELECT 1 FROM task_jobs WHERE task_id=? AND state='running' LIMIT 1",(tid,)).fetchone():
                        return self.send_json({'error':'Обработчик ещё завершает задачу. Повторите после его остановки.'},409)
                    if con.execute("SELECT 1 FROM operations WHERE task_id=? AND state IN ('sending','unknown') LIMIT 1",(tid,)).fetchone():
                        return self.send_json({'error':'Сначала разрешите операции с неизвестным исходом.'},409)
                operations.allow_confirmed_unsent_retries(tid,con)
                task.pop('watchdog_timeout',None)
                task.update(status='queued',stage='setup',error=None,message='Возобновление с сохранённого этапа');_save(con,task)
                if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':submit_task(tid,not bool(task.get('query')))
            if os.getenv('TASK_QUEUE_BACKEND','local')!='postgres':submit_task(tid,not bool(task.get('query')))
            return self.send_json({'ok':True})
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/query", path)
        if match:
            query = str(self.read_json().get("query") or "").strip()
            if not query or len(query) > 4000:
                return self.send_json({"error": "Запрос должен содержать от 1 до 4000 символов."}, 400)
            task=get_task(match.group(1))
            if task and task['mode']=='xray':
                try:query=validate_plan(query,task.get('xray_sources',DEFAULT_SOURCES))
                except ValueError as exc:return self.send_json({'error':str(exc)},400)
            if not save_query(match.group(1), query):
                return self.send_json({"error": "Запрос уже подтверждён, отменён или срок ожидания истёк."}, 409)
            return self.send_json({"ok": True})
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/continue", path)
        if match:
            task=get_task(match.group(1))
            if task and task['mode']=='xray':
                try:validate_plan(task.get('query',''),task.get('xray_sources',DEFAULT_SOURCES))
                except ValueError as exc:return self.send_json({'error':str(exc)},400)
            if not claim_review(match.group(1)):
                return self.send_json({"error": "Задача уже запущена или недоступна."}, 409)
            try:
                if os.getenv('TASK_QUEUE_BACKEND','local')!='postgres':submit_task(match.group(1), False)
            except Exception:
                update_task(match.group(1), status="failed", stage="failed", error="Не удалось поставить задачу в очередь.")
                return self.send_json({"error": "Не удалось поставить задачу в очередь."}, 503)
            return self.send_json({"ok": True})
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})/cancel", path)
        if match:
            ok = cancel_review(match.group(1))
            return self.send_json({"ok": True} if ok else {"error": "Отмена недоступна."}, 200 if ok else 409)
        match = re.fullmatch(r"/api/vacancy/(\d+)/(find-resumes|xray-search)", path)
        if match:
            data = self.read_json()
            try:
                requested = data.get("requested_count", DEFAULT_EVALUATION_COUNT)
                if type(requested) is not int or not 1 <= requested <= 10: raise ValueError()
            except (TypeError, ValueError):
                return self.send_json({"error": "Укажите целое количество от 1 до 10."}, 400)
            email = str(data.get("responsible_email") or "").strip()
            if not valid_email(email):
                return self.send_json({"error": "Укажите корректную почту ответственного лица."}, 400)
            mode = "xray" if match.group(2) == "xray-search" else "hh"
            sources=None
            if mode=='xray':
                try:sources=validate_sources(data.get('xray_sources',DEFAULT_SOURCES))
                except ValueError as exc:return self.send_json({'error':str(exc)},400)
            task_id, created = start_task(match.group(1), requested, email, mode, sources,owner_session=identity(self.headers))
            if not owns(get_task(task_id),identity(self.headers)):
                return self.send_json({'error':'Активная задача уже принадлежит другой сессии.'},409)
            return self.send_json({"task_id": task_id, "created": created}, 202)
        self.send_json({"error": "Not found"}, 404)


_instance_handle = None

def acquire_instance_lock():
    # Recovery must never classify another live worker as crashed.
    global _instance_handle
    from app_state import DB_PATH
    handle=open(str(DB_PATH)+'.lock','a+b')
    try:
        import os
        if os.name=='nt':
            import msvcrt
            handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,IOError):
        handle.close()
        raise RuntimeError('Другой экземпляр уже использует эту базу. Закройте его перед запуском.')
    _instance_handle=handle

def start_server():
    acquire_instance_lock()
    # Import the user's existing history once; do not silently start with an empty DB.
    legacy = BASE_DIR / "resume_ai_history.xlsx"
    if not legacy.exists(): legacy = BASE_DIR / "data" / "resume_ai_history.xlsx"
    try:
        imported = import_legacy_history(legacy)
        if imported: logger.info("Импортировано строк истории: %s", imported)
    except Exception:
        logger.exception("Не удалось импортировать старую историю")
        raise RuntimeError("Не удалось импортировать историю Excel. Закройте файл и проверьте его перед запуском.")
    from retention import start_retention
    retention_stop,retention_thread=start_retention()
    if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        from durable_queue import init,reap,reconcile
        from shared_screen import init as init_screens
        init();init_screens();reap();reconcile()
        recovered=[]  # Never globally reset jobs owned by live workers.
    else:
        recovered = recover_tasks()
    for tid in recovered:
        task=get_task(tid)
        submit_task(tid,not bool(task.get('query')))
    port = APP_PORT
    while True:
        with socket.socket() as probe:
            if probe.connect_ex((APP_HOST, port)) != 0:
                break
        port += 1
    server = ThreadingHTTPServer((APP_HOST, port), Handler)
    url = f"http://{APP_HOST}:{port}/"
    print(f"[STARTUP] Веб-сервер запущен: {url}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        retention_stop.set()
        retention_thread.join(timeout=2)
