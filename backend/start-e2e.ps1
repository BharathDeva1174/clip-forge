$ErrorActionPreference = 'Stop'

# Start the API and the worker for a manual end-to-end run, logging each process
# to a file (never a pipe: a full pipe blocks the writer and looks like a hang).
# Paths are resolved from this script's own location, so the clone can sit
# anywhere on any machine.

$env:HOME = [System.Environment]::GetFolderPath('UserProfile')
$env:CLIPFORGE_PORT = '8000'

$BackendDir = $PSScriptRoot
$RepoRoot = Resolve-Path (Join-Path $BackendDir '..')
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "No virtual environment at $Python - create it with: python -m venv ..\.venv"
}

$LogRoot = Join-Path $BackendDir 'data\temp\e2e-logs'
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

Start-Process -FilePath $Python -ArgumentList 'run.py' `
    -RedirectStandardOutput (Join-Path $LogRoot 'api.log') `
    -RedirectStandardError  (Join-Path $LogRoot 'api.err') `
    -WorkingDirectory $BackendDir
Start-Sleep -Seconds 8
Start-Process -FilePath $Python -ArgumentList 'worker.py' `
    -RedirectStandardOutput (Join-Path $LogRoot 'worker.log') `
    -RedirectStandardError  (Join-Path $LogRoot 'worker.err') `
    -WorkingDirectory $BackendDir

Write-Output "API and worker started; logs in $LogRoot"
