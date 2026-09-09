# Install the built NSIS bundle and exercise its packaged backend in an isolated home.
# Provisioning downloads Python/dependencies; the smoke itself calls only loopback HTTP.
param([Parameter(Mandatory)][string]$Installer)
$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$smokeRoot = Join-Path ([IO.Path]::GetTempPath()) ("agency-smoke-" + [guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $smokeRoot 'installed'
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$cockpit = $null
$savedEnv = @{}
foreach ($name in @('AIMEAT_HOME','AIMEAT_AGENCY_TOKEN','AIMEAT_AGENCY_PORT','AIMEAT_AGENCY_HOST','OTEL_SDK_DISABLED','CREWAI_TELEMETRY_DISABLED')) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $setup = Start-Process -FilePath $installerPath -ArgumentList @('/S', "/D=$installRoot") -WindowStyle Hidden -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "Installer failed: $($setup.ExitCode)" }
    if (-not (Test-Path -LiteralPath (Join-Path $installRoot 'aimeat-agency.exe'))) { throw 'Installed shell missing' }
    $runtime = @(Get-ChildItem -LiteralPath $installRoot -Directory -Recurse | Where-Object Name -eq 'runtime-src')
    $uv = @(Get-ChildItem -LiteralPath $installRoot -File -Recurse -Filter 'uv.exe')
    if ($runtime.Count -ne 1 -or $uv.Count -ne 1) { throw 'Expected one packaged runtime and one uv sidecar' }
    $runtimePath = $runtime[0].FullName
    foreach ($file in @('pyproject.toml','uv.lock','src/crewaimeat/agency/static/index.html')) {
        if (-not (Test-Path -LiteralPath (Join-Path $runtimePath $file))) { throw "Packaged resource missing: $file" }
    }
    & $uv[0].FullName sync --frozen --extra agency --no-dev --project $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Packaged runtime provisioning failed' }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $env:AIMEAT_HOME = Join-Path $smokeRoot 'home'
    $env:AIMEAT_AGENCY_TOKEN = [guid]::NewGuid().ToString('N')
    $env:AIMEAT_AGENCY_PORT = "$port"
    $env:AIMEAT_AGENCY_HOST = '127.0.0.1'
    $env:OTEL_SDK_DISABLED = 'true'
    $env:CREWAI_TELEMETRY_DISABLED = 'true'
    $python = Join-Path $runtimePath '.venv/Scripts/python.exe'
    $cockpit = Start-Process -FilePath $python -ArgumentList @('-m','crewaimeat.agency.cockpit') -WorkingDirectory $runtimePath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $smokeRoot 'cockpit.log') -RedirectStandardError (Join-Path $smokeRoot 'cockpit.err.log')
    $base = "http://127.0.0.1:$port"
    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        if ($cockpit.HasExited) { throw 'Packaged cockpit exited before readiness' }
        try { $ready = (Invoke-RestMethod "$base/healthz" -TimeoutSec 2).ok } catch { $ready = $false }
        if ($ready) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Packaged cockpit did not become ready' }
    $denied = Invoke-WebRequest "$base/api/brains" -SkipHttpErrorCheck
    if ($denied.StatusCode -ne 401) { throw 'Missing token was not rejected' }
    $headers = @{Authorization = "Bearer $env:AIMEAT_AGENCY_TOKEN"}
    $templates = Invoke-RestMethod "$base/api/templates" -Headers $headers
    if (-not $templates.templates) { throw 'Packaged templates missing' }
    $body = @{agent_name='smoke-agent'; template_id='topic-watcher'} | ConvertTo-Json
    $brain = Invoke-RestMethod "$base/api/brains" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
    if ($brain.agent_name -ne 'smoke-agent') { throw 'Brain creation failed' }
    $read = Invoke-RestMethod "$base/api/brains/smoke-agent" -Headers $headers
    if ($read.version -ne $brain.version) { throw 'Persisted brain differs' }
    $memory = Invoke-RestMethod "$base/api/memory/smoke-agent" -Headers $headers
    if ($null -eq $memory.records) { throw 'Memory route missing' }
    $page = Invoke-WebRequest "$base/?boot=$env:AIMEAT_AGENCY_TOKEN"
    if ($page.Content -match '__AGENCY_TOKEN__') { throw 'Cockpit token injection failed' }
    Invoke-RestMethod "$base/api/brains/smoke-agent" -Method Delete -Headers $headers | Out-Null
    Write-Host 'Installer smoke passed: bundle resources, provisioning, health, auth, brain persistence, memory and HTML.'
} catch {
    foreach ($log in @('cockpit.log','cockpit.err.log')) {
        $logPath = Join-Path $smokeRoot $log
        if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Tail 80 }
    }
    throw
} finally {
    if ($cockpit -and -not $cockpit.HasExited) { Stop-Process -Id $cockpit.Id -Force }
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
    # Leave logs/artifacts in this unique temporary directory for a failed-run diagnosis.
    Write-Host "Smoke diagnostics: $smokeRoot"
}
