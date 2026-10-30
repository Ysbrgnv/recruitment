from runtime_config import load_runtime
load_runtime()
if __name__=='__main__':
    import os
    import sys
    if '--lane' in sys.argv and sys.argv[sys.argv.index('--lane')+1]=='browser' and not os.getenv('BROWSER_WS_ENDPOINT'):
        raise SystemExit('BROWSER_WS_ENDPOINT is required for browser workers')
    os.environ["PROCESS_ROLE"]="worker-"+sys.argv[sys.argv.index("--lane")+1]
    from logging_setup import configure_logging
    configure_logging()
    from durable_queue import main
    main()

