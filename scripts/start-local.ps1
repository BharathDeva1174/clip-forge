#Requires -Version 5.1
<#
.SYNOPSIS
    Launch ClipForge locally: the API, the background worker and the frontend.

.DESCRIPTION
    Starts the three ClipForge processes in order, waits for each one to become
    ready, and leaves them running in the current terminal.

    The API and the worker are separate processes: the API only queues work,
    and the worker is the only thing that runs FFmpeg, transcription and AI
    requests. Both must be running for a job to reach `completed`.

    The script starts each process in its own PowerShell job so that one
    terminal can show all three and wait for readiness. Stop them with
    Ctrl+C in this terminal, or run `Stop-ClipForge` after sourcing this
    script.

.PARAMETER BackendDir
    Folder that contains `run.py` and `worker.py`. Defaults to `backend` next
    to this script.

.PARAMETER FrontendDir
    Folder that contains `package.json`. Defaults to `frontend` next to this
    script.

.PARAMETER Python
    The Python executable to use. Defaults to `..\.venv\Scripts\python.exe`
    relative to `BackendDir` (the ClipForge virtual environment).

.PARAMETER ApiPort
    Port for the API. Defaults to whatever `CLIPFORGE_PORT` says, or 8000.

.PARAMETER FrontendPort
    Port for the frontend. Defaults to whatever `NEXT_PUBLIC_API_BASE_URL`
    implies, or 3000.

.EXAMPLE
    .\scripts\start-local.ps1

    Starts the API on http://127.0.0.1:8000 and the frontend on
    http://localhost:3000, and waits until both report ready.

.EXAMPLE
    .\scripts\start-local.ps1 -ApiPort 8010 -FrontendPort 3001

    Starts on non-default ports. Make sure `CLIPFORGE_PORT` and
    `NEXT_PUBLIC_API_BASE_URL` match if you want the frontend to reach the API.

.NOTES
    Child-process stdout goes to log files (not to pipes), because a Windows
    pipe holds only a few KB and a server that fills it blocks while writing
    its log -- which looks exactly like the server having hung.

    Each process writes a log into $env:TEMP/clipforge-local-launch/:
        clipforge-api.log
        clipforge-worker.log
        clipforge-frontend.log

    The environment files backend/.env and frontend/.env.local must exist
    (copy the *.example templates). The script never creates them.

#>
[CmdletBinding()]
param(
    [string]$BackendDir = "",
    [string]$FrontendDir = "",
    [string]$Python = "",
    [int]$ApiPort = 0,
    [int]$FrontendPort = 0,
    [switch]$Replace
)

$ErrorActionPreference = "Stop"

# Resolve script-relative paths in the body: $PSScriptRoot is not reliable in
# parameter default expressions on every host, so compute them here.
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $PSCommandPath }
$ScriptDir = Resolve-Path $ScriptDir
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
if (-not $BackendDir) { $BackendDir = Join-Path $RepoRoot "backend" }
if (-not $FrontendDir) { $FrontendDir = Join-Path $RepoRoot "frontend" }
if (-not $Python) { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }
$LogRoot = Join-Path $env:TEMP "clipforge-local-launch"
# Keep the previous run's logs (renamed to *.prev.log) instead of deleting
# them: the log of the run that went wrong is exactly what is needed when
# diagnosing a failure after a restart.
if (Test-Path $LogRoot) {
    Get-ChildItem $LogRoot -Filter "clipforge-*.log" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Move-Item -Path $_.FullName -Destination ($_.FullName -replace '\.log$', '.prev.log') -Force
        }
}
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

# Resolve ports from environment when not given on the command line.
if ($ApiPort -le 0) {
    $envApiPort = [int]::TryParse($env:CLIPFORGE_PORT, [ref]0)
    $ApiPort = if ($envApiPort) { [int]$env:CLIPFORGE_PORT } else { 8000 }
}
if ($FrontendPort -le 0) {
    $FrontendPort = 3000
}

$ApiUrl = "http://127.0.0.1:$ApiPort"
$FrontendUrl = "http://localhost:$FrontendPort"

# run.py takes its port from CLIPFORGE_PORT, not from a command-line flag, so
# set it in this session's environment before launching. Start-Process inherits
# the caller's environment and has no -Environment parameter of its own.
$env:CLIPFORGE_PORT = $ApiPort.ToString()

function Wait-ForTcp {
    param([string]$Address, [int]$Port, [int]$TimeoutSeconds = 90)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $socket = New-Object System.Net.Sockets.TcpClient
            $socket.Connect($Address, $Port)
            $socket.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    return $false
}

function Wait-ForHttp {
    param([string]$Url, [string]$Path = "/health", [int]$TimeoutSeconds = 90, [string]$Expect = "ok")
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri "$Url$Path" -Method Get -TimeoutSec 5 -ErrorAction Stop
            if ($response.status -eq $Expect) {
                return $true
            }
        } catch {
            # The service is not listening yet.
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Start-LoggedProcess {
    param([string]$Name, [string]$Command, [string[]]$ArgumentList, [string]$WorkingDirectory)
    $logPath = Join-Path $LogRoot "clipforge-$Name.log"
    $errPath = Join-Path $LogRoot "clipforge-$Name.err.log"
    # Stdout/stderr go to files, never to pipes. A Windows pipe holds only ~4
    # KB, and a server that fills its pipe blocks while writing its log --
    # which looks exactly like the server having hung. Start-Process requires
    # the two streams to go to different files.
    #
    # Environment variables are applied to the current PowerShell session before
    # calling this function, because Start-Process inherits the caller's
    # environment and has no -Environment parameter of its own.
    $process = Start-Process -FilePath $Command -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory `
        -PassThru -RedirectStandardOutput $logPath -RedirectStandardError $errPath
    return $process
}

function Stop-ClipForge {
    <#
    .SYNOPSIS
        Stop the ClipForge processes started by this script.
    #>
    param([switch]$Force)
    $processes = @($script:ClipForgeApiProcess, $script:ClipForgeWorkerProcess, $script:ClipForgeFrontendProcess)
    foreach ($process in $processes) {
        if ($null -eq $process -or $process.HasExited) { continue }
        if ($Force) {
            $process.Kill()
        } else {
            $process.CloseMainWindow() | Out-Null
        }
        try {
            $process.WaitForExit(20000) | Out-Null
        } catch { }
        if (-not $process.HasExited) {
            $process.Kill() | Out-Null
        }
    }
}

# --- validate prerequisites -------------------------------------------------
if (-not (Test-Path (Join-Path $BackendDir "run.py"))) {
    Write-Error "Cannot find $BackendDir\run.py. Run this script from the repository root."
}
if (-not (Test-Path $Python)) {
    Write-Error "Cannot find the Python executable at $Python. Create the virtual environment first (docs/DEVELOPMENT.md)."
}

# --- pre-flight: refuse to stack on ClipForge processes that are running ----
# Several workers sharing one database and one heartbeat file produce lease
# churn, a /ready report that flaps between worker ids, and analyses that
# lose their lease mid-provider-call. One API and one worker is the shape
# the system is built around; this stops the accumulation before it starts.
$stale = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'worker\.py' -or $_.CommandLine -match 'run\.py') })
if ($stale.Count -gt 0) {
    $staleIds = ($stale | ForEach-Object { $_.ProcessId }) -join ", "
    if ($Replace) {
        foreach ($process in $stale) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
        Write-Host "     Stopped $($stale.Count) pre-existing ClipForge python process(es): $staleIds" -ForegroundColor Yellow
    } else {
        Write-Error ("ClipForge python processes are already running (PIDs: $staleIds). " +
            "A second API cannot bind the port and extra workers corrupt the lease bookkeeping. " +
            "Stop them first, or re-run with -Replace to have this script stop them.")
    }
}
if (-not (Test-Path (Join-Path $BackendDir ".env"))) {
    Write-Warning "backend/.env is missing. Copy backend/.env.example to backend/.env first."
}
if (-not (Test-Path (Join-Path $FrontendDir ".env.local"))) {
    Write-Warning "frontend/.env.local is missing. Copy frontend/.env.example to frontend/.env.local first."
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg is not on PATH. Media processing will fail until FFmpeg is installed."
}
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "ffprobe is not on PATH. Media validation will fail until FFprobe is installed."
}

# --- Terminal 1: API --------------------------------------------------------
Write-Host "==> Starting the ClipForge API on $ApiUrl" -ForegroundColor Cyan
$script:ClipForgeApiProcess = Start-LoggedProcess -Name api -Command $Python `
    -ArgumentList @("run.py") -WorkingDirectory $BackendDir
if (-not (Wait-ForTcp -Address 127.0.0.1 -Port $ApiPort -TimeoutSeconds 90)) {
    Write-Error "The API did not start listening on $ApiUrl. See $LogRoot\clipforge-api.log."
}
if (-not (Wait-ForHttp -Url $ApiUrl -Path "/health" -TimeoutSeconds 90)) {
    Write-Error "The API is listening but did not answer /health. See $LogRoot\clipforge-api.log."
}
Write-Host "     API is up: $ApiUrl/health" -ForegroundColor Green

# --- Terminal 2: worker -----------------------------------------------------
Write-Host "==> Starting the background worker" -ForegroundColor Cyan
$script:ClipForgeWorkerProcess = Start-LoggedProcess -Name worker -Command $Python `
    -ArgumentList @("worker.py") -WorkingDirectory $BackendDir
# The worker writes a heartbeat; /ready reports whether it looks alive.
if (-not (Wait-ForHttp -Url $ApiUrl -Path "/ready" -TimeoutSeconds 90 -Expect "ready")) {
    Write-Error "The worker did not report ready through /ready. See $LogRoot\clipforge-worker.log."
}
# Give the worker a moment to write its first heartbeat after the API reports
# /ready, then confirm the heartbeat shows the worker as running.
$workerRunning = $false
for ($i = 0; $i -lt 20; $i++) {
    $ready = Invoke-RestMethod -Method Get -Uri "$ApiUrl/ready" -TimeoutSec 10 -ErrorAction Stop
    if ($ready.worker.running -eq $true) {
        $workerRunning = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $workerRunning) {
    Write-Error "The worker heartbeat is not running. The API is up but jobs will stay queued."
}
Write-Host "     Worker is running ($($ready.worker.worker_id))" -ForegroundColor Green

# --- Terminal 3: frontend ---------------------------------------------------
Write-Host "==> Starting the frontend on $FrontendUrl" -ForegroundColor Cyan
if (Wait-ForTcp -Address localhost -Port $FrontendPort -TimeoutSeconds 2) {
    # Something already serves the port -- most often a dev server from an
    # earlier session. Starting a second one dies with EADDRINUSE while the
    # launcher reports success about a process it does not own. Reuse the
    # running server instead, and say so.
    Write-Host "     Port $FrontendPort is already serving; reusing that frontend (it was not started here, so Stop-ClipForge will not stop it)." -ForegroundColor Yellow
    $script:ClipForgeFrontendProcess = $null
} else {
    # The frontend reads NEXT_PUBLIC_API_BASE_URL from its own environment at
    # startup, so set it in this session before launching (Start-Process inherits
    # the caller's environment).
    $env:NEXT_PUBLIC_API_BASE_URL = $ApiUrl
    # npm on Windows ships as npm.cmd; use it directly so Start-Process can exec it.
    $npmCmd = Join-Path (Split-Path -Parent (Get-Command npm -ErrorAction SilentlyContinue).Source) "npm.cmd"
    if (-not (Test-Path $npmCmd)) { Write-Error "npm.cmd is not on PATH. Install Node.js first." }
    $script:ClipForgeFrontendProcess = Start-LoggedProcess -Name frontend -Command $npmCmd `
        -ArgumentList @("run", "dev", "--", "-p", $FrontendPort) -WorkingDirectory $FrontendDir
    if (-not (Wait-ForTcp -Address localhost -Port $FrontendPort -TimeoutSeconds 120)) {
        Write-Error "The frontend did not start listening on $FrontendUrl. See $LogRoot\clipforge-frontend.log."
    }
}
# The dev server answers with HTML; confirming the port is open is enough to
# say the frontend is up.
Write-Host "     Frontend is up: $FrontendUrl" -ForegroundColor Green

Write-Host ""
Write-Host "==> ClipForge is running" -ForegroundColor Green
Write-Host "     API         : $ApiUrl"
Write-Host "     API health  : $ApiUrl/health"
Write-Host "     API ready   : $ApiUrl/ready"
Write-Host "     Frontend    : $FrontendUrl"
Write-Host "     Logs        : $LogRoot"
Write-Host ""
Write-Host "Stop all three processes with Ctrl+C in this terminal, or run Stop-ClipForge." -ForegroundColor Gray
