@echo off
setlocal
REM AgentsServer auto-start for Windows (no systemd equivalent required).
REM Ensures the CLIs the server shells out to are on PATH, then serves on 7850.
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.kimi-code\bin;%APPDATA%\npm;%PATH%"
cd /d "%USERPROFILE%\AgentsServer"
if not exist ".venv\Scripts\python.exe" (
  "%USERPROFILE%\.local\bin\uv.exe" sync --frozen
)
set "AGENTSDOCK_STATE_DIR=%USERPROFILE%\.agentsdock"
REM Load optional KEY=VALUE defaults from %AGENTSDOCK_STATE_DIR%\server.env
REM (mode 0600). Variables already set in the environment take precedence;
REM values are never echoed. The agent token lives only in server.env.
if exist "%AGENTSDOCK_STATE_DIR%\server.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%AGENTSDOCK_STATE_DIR%\server.env") do (
    if not defined %%A set "%%A=%%B"
  )
)
"%USERPROFILE%\.local\bin\uv.exe" run python agent_server.py serve --bind 0.0.0.0 --port 7850 >> "%USERPROFILE%\AgentsServer\server.log" 2>&1
