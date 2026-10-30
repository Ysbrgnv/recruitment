$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$compose = @("compose", "--env-file", "docker.local.env", "-p", "recruitment-clean-v3-parallel")
docker @compose build web
if ($LASTEXITCODE -ne 0) { throw "Build failed; running containers unchanged." }
docker @compose up -d --no-deps --no-build --force-recreate web
if ($LASTEXITCODE -ne 0) { throw "Web update failed. Check Docker output." }
Write-Host "Web updated. Database and workers were not recreated."
