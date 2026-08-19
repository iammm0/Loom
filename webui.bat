@echo off
setlocal
set "CURRENT_DIR=%CD%"
set "PYTHONPATH=%CURRENT_DIR%"

if not defined MPT_WEBUI_HOST set "MPT_WEBUI_HOST=127.0.0.1"
if not defined MPT_API_PORT set "MPT_API_PORT=8080"

if exist "%CURRENT_DIR%\webui\package.json" (
    if not exist "%CURRENT_DIR%\webui\node_modules" (
        pushd "%CURRENT_DIR%\webui"
        call npm install
        popd
    )
    if not exist "%CURRENT_DIR%\webui\dist\index.html" (
        pushd "%CURRENT_DIR%\webui"
        call npm run build
        popd
    )
)

echo ***** WebUI address: http://%MPT_WEBUI_HOST%:%MPT_API_PORT% *****

if exist "%CURRENT_DIR%\.venv\Scripts\python.exe" (
    "%CURRENT_DIR%\.venv\Scripts\python.exe" "%CURRENT_DIR%\main.py"
    goto :eof
)
if exist "%CURRENT_DIR%\lib\python\python.exe" (
    "%CURRENT_DIR%\lib\python\python.exe" "%CURRENT_DIR%\main.py"
    goto :eof
)
where uv >nul 2>nul
if not errorlevel 1 (
    uv run python "%CURRENT_DIR%\main.py"
    goto :eof
)
python "%CURRENT_DIR%\main.py"
