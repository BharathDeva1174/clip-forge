@echo off
rem Start the ClipForge API and worker from this machine's virtual environment.
rem scripts\start-local.ps1 is the preferred launcher: it readiness-gates each
rem process, starts the frontend too, and logs each process to a file.
set HOME=%USERPROFILE%
set CLIPFORGE_PORT=8000
set PYTHON=%~dp0..\.venv\Scripts\python.exe
start "" "API" "%PYTHON%" run.py
timeout /t 5
start "" "WORKER" "%PYTHON%" worker.py
