from app.core.runtime_config import load_runtime
load_runtime()
if __name__=='__main__':
    import os,uvicorn
    os.environ['PROCESS_ROLE']='web'
    config=uvicorn.Config('app.web.asgi_app:app',host=os.getenv('APP_HOST','127.0.0.1'),port=int(os.getenv('APP_PORT','8765')),
        workers=1,limit_concurrency=64,timeout_keep_alive=5,timeout_graceful_shutdown=30,
        proxy_headers=False,access_log=False)
    server=uvicorn.Server(config)
    if os.name=='nt':
        import signal
        signal.signal(signal.SIGBREAK,server.handle_exit)
    server.run()

