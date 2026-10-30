import sys,urllib.request,json
try:
    with urllib.request.urlopen('http://127.0.0.1:8765/health/ready',timeout=5) as r:
        ok=r.status==200 and json.load(r).get('ready') is True
except Exception:ok=False
sys.exit(0 if ok else 1)

