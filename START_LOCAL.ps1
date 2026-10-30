$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot
function Run-Docker {
    param([string]$Title,[string[]]$Arguments)
    Write-Host "`n==> $Title" -ForegroundColor Cyan
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Title failed with exit code $LASTEXITCODE. The Docker details are immediately above. Do not send .env." }
}
if (-not (Test-Path ".env" -PathType Leaf)) { throw "Place .env next to START_LOCAL.ps1." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI was not found. Start Docker Desktop." }
Run-Docker "Checking Docker engine" @("info","--format","{{.ServerVersion}}")
$project="recruitment-clean-v3-parallel"
$compose=@("compose","--env-file","docker.local.env","-p",$project)
Run-Docker "Building application image" @("build","--target","app","-t","recruitment-clean-app:v3-parallel",".")
Run-Docker "Building browser image" @("build","-f","browser_service/Dockerfile","-t","recruitment-clean-browser:v3-parallel",".")
Run-Docker "Preparing local secrets" @("run","--rm","--network","none","--user","0:0","--entrypoint","python","--mount","type=bind,source=$PSScriptRoot,target=/setup","recruitment-clean-app:v3-parallel","/setup/docker/prepare_local.py")
Run-Docker "Validating Compose" ($compose+@("config","--quiet"))
Run-Docker "Stopping application writers" ($compose+@("stop","web","worker-general","worker-browser","browser","init-config"))
Run-Docker "Starting PostgreSQL" ($compose+@("up","-d","--no-build","--wait","--wait-timeout","180","db"))
$tableCount=& docker @compose exec -T db psql -U postgres -d recruitment -Atc "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'"
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect PostgreSQL schema." }
if ($tableCount.Trim() -eq "0") { Run-Docker "Bootstrapping database" ($compose+@("--profile","admin","run","--rm","--no-deps","bootstrap")) }
Run-Docker "Synchronizing runtime configuration" ($compose+@("up","--no-build","--force-recreate","--no-color","init-config"))
Run-Docker "Starting services" ($compose+@("up","-d","--no-build","--force-recreate","--wait","--wait-timeout","240","browser","web","worker-general","worker-browser"))
Run-Docker "Testing browser isolation" ($compose+@("run","--rm","--no-deps","web","smoke-browser"))
Run-Docker "Displaying service status" ($compose+@("ps"))
Write-Host "`nREADY: http://localhost:8765" -ForegroundColor Green
Write-Host "Parallelism: HH=16, X-Ray=8. Existing database and rotated HH tokens are preserved on the next START_LOCAL run."

