"""Production ASGI transport; existing business routes reused without an HTTP socket.
The legacy Handler is a compatibility dispatcher only, not a listening server.
CAPTCHA streaming is a native bounded StreamingResponse.
"""
import asyncio,io,json,os,secrets,time,logging
from contextlib import asynccontextmanager
from email.message import Message
from pathlib import Path
from urllib.parse import urlparse
import anyio
from fastapi import FastAPI,Request
from starlette.responses import Response,JSONResponse,StreamingResponse
log=logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    from logging_setup import configure_logging
    configure_logging()
    anyio.to_thread.current_default_thread_limiter().total_tokens=16
    from durable_queue import init,reap,reconcile
    from shared_screen import init as screen_init
    from telemetry import init as metric_init
    from retention import start_retention
    await anyio.to_thread.run_sync(lambda:(init(),screen_init(),metric_init(),reap(),reconcile()))
    stop,thread=start_retention()
    # Never open an interactive HH login from a server startup hook.
    from web_server import load_vacancies,APP_STATE
    if os.getenv('HH_BEARER') or os.getenv('HH_REFRESH_TOKEN'):
        async def load():await anyio.to_thread.run_sync(load_vacancies)
        task=asyncio.create_task(load())
    else:
        APP_STATE['startup_error']='Не настроен токен HH.';task=None
    try:yield
    finally:
        stop.set();await anyio.to_thread.run_sync(lambda:thread.join(timeout=5))
        if task:
            try:await asyncio.wait_for(asyncio.shield(task),timeout=5)
            except (TimeoutError,asyncio.CancelledError):task.cancel()
        from db_pool import close_pools
        await anyio.to_thread.run_sync(close_pools)

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/health/live')
def live():return {'status':'alive'}

def readiness():
    checks={'database':False,'browser':False,'general_workers':False,'browser_workers':False}
    try:
        from durable_queue import connect
        with connect() as c:
            c.execute('SELECT next_run,last_error FROM task_jobs LIMIT 0')
            c.execute('SELECT 1 FROM service_counters LIMIT 0');checks['database']=True
            for r in c.execute("SELECT DISTINCT lane FROM worker_presence WHERE updated>%s AND state<>'stopped'",(time.time()-60,)):
                checks[r['lane']+'_workers']=True
    except Exception:log.warning('Database readiness failed')
    try:
        import requests
        url=os.environ.get('BROWSER_HEALTH_URL','http://127.0.0.1:3001/health/ready')
        r=requests.get(url,timeout=(1,2));checks['browser']=r.status_code==200 and r.json().get('ready') is True
    except Exception:pass
    return checks

@app.get('/health/ready')
def ready():
    checks=readiness();ok=all(checks.values())
    return JSONResponse({'ready':ok,'checks':checks},status_code=200 if ok else 503)

@app.get('/metrics')
def metrics(request:Request):
    token=os.getenv('METRICS_TOKEN','')
    if not token or not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
        return Response(status_code=403)
    from telemetry import render
    return Response(render(),media_type='text/plain; version=0.0.4')

@app.get('/api/tasks/{task_id}/screen/stream')
async def screen_stream(task_id:str,request:Request):
    import re
    if not re.fullmatch('[a-f0-9]{32}',task_id):return JSONResponse({'error':'Not found'},404)
    from api_guard import identity,owns
    from app_state import get_task
    import browser_screen
    task=await anyio.to_thread.run_sync(lambda:get_task(task_id))
    if not owns(task,identity(request.headers)):return JSONResponse({'error':'Задача недоступна для этой сессии.'},403)
    token=request.query_params.get('token','')
    if not await anyio.to_thread.run_sync(lambda:browser_screen.open_stream(task_id,token)):
        return JSONResponse({'error':'Проверка завершена или лимит подключений исчерпан.'},409)
    async def frames():
        seq=-1;deadline=time.monotonic()+120
        try:
            while time.monotonic()<deadline and not await request.is_disconnected():
                item=await anyio.to_thread.run_sync(lambda:browser_screen.next_frame(task_id,token,seq,1))
                if item is None:break
                if not item['frame']:continue
                seq=item['seq'];data=bytes(item['frame'])
                yield b'--screenframe\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n'
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda:browser_screen.close_stream(task_id,token))
    return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=screenframe',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

def dispatch(method,path,headers,body):
    from web_server import Handler
    class Adapter(Handler):
        def __init__(self):
            self.path=path;self.command=method;self.headers=Message()
            # ASGI has already decoded transport framing and buffered the body.
            for k, v in headers:
                name = k.decode('latin1')
                if name.lower() in ('transfer-encoding', 'content-length'):
                    continue
                self.headers[name] = v.decode('latin1')
            self.headers['Content-Length'] = str(len(body))
            self.rfile=io.BytesIO(body);self.wfile=io.BytesIO();self.status=200;self.response_headers=[]
            self.connection=type('Transport',(),{'settimeout':lambda self,n:None})()
        def send_response(self,status,*_):self.status=status
        def send_header(self,key,value):self.response_headers.append((key,value))
        def end_headers(self):pass
    h=Adapter()
    if method in ('GET','HEAD'):h.do_GET()
    elif method=='POST':h.do_POST()
    else:return Response(status_code=405)
    response=Response(h.wfile.getvalue() if method!='HEAD' else b'',status_code=h.status)
    response.raw_headers=[(k.lower().encode('latin1'),v.encode('latin1')) for k,v in h.response_headers]
    return response

@app.api_route('/{path:path}',methods=['GET','HEAD','POST'])
async def routes(path:str,request:Request):
    body=bytearray()
    try:
        async def read():
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>65536:raise ValueError('large')
        await asyncio.wait_for(read(),timeout=10)
    except ValueError:return JSONResponse({'error':'Размер запроса превышает 64 КиБ'},413)
    except TimeoutError:return JSONResponse({'error':'Истекло время чтения запроса'},408)
    target=request.url.path+('?' + request.url.query if request.url.query else '')
    try:
        return await anyio.to_thread.run_sync(lambda:dispatch(request.method,target,request.headers.raw,bytes(body)))
    except Exception:
        log.exception('HTTP request failed')
        from telemetry import increment
        await anyio.to_thread.run_sync(lambda:increment('http_errors_total'))
        return JSONResponse({'error':'Внутренняя ошибка сервера'},500)

