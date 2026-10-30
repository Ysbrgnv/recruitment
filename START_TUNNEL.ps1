$ErrorActionPreference = "Stop"
Invoke-RestMethod -Uri "http://127.0.0.1:8765/health/live" -TimeoutSec 5
cloudflared tunnel --protocol http2 --url http://127.0.0.1:8765
