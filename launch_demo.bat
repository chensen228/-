@echo off
cd /d "%~dp0"
set "REDIS_DIR=%~dp0..\tools\redis_portable\Redis"
set "REDIS_RUNNING="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":6379" ^| findstr "LISTENING"') do set "REDIS_RUNNING=1"
if not defined REDIS_RUNNING (
  if exist "%REDIS_DIR%\redis-server.exe" (
    start "RedisPortable" "%REDIS_DIR%\redis-server.exe" "%REDIS_DIR%\redis.windows.conf"
    timeout /t 2 >nul
  )
)
start http://127.0.0.1:5050
python app.py
