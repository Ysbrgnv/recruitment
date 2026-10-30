$ErrorActionPreference="Stop"
Set-Location $PSScriptRoot
$compose=@("compose","--env-file","docker.local.env","-p","recruitment-clean-v3-parallel")
docker @compose stop web worker-general worker-browser
if ($LASTEXITCODE -ne 0) { throw "Workers did not stop cleanly." }
docker @compose stop browser db init-config
if ($LASTEXITCODE -ne 0) { throw "Stop failed." }

