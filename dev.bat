@echo off
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "UV_PROJECT_ENVIRONMENT=%ROOT%\.venv-video-loom"
set "VENV_PY=%ROOT%\.venv-video-loom\Scripts\python.exe"
set "PYTHONPATH=%ROOT%"

echo [dev] API hot-reload: http://127.0.0.1:8080
echo [dev] WebUI HMR:      http://127.0.0.1:8501
echo.

if not exist "%ROOT%\webui\node_modules" (
  pushd "%ROOT%\webui"
  call npm install
  popd
)

start "video-loom-api" cmd /k "cd /d "%ROOT%" && set PYTHONPATH=%ROOT% && "%VENV_PY%" main.py"
start "video-loom-webui" cmd /k "cd /d "%ROOT%\webui" && npm run dev"

echo Started API + WebUI in new windows.
echo Close those windows to stop.
