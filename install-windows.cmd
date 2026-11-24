@echo off
setlocal
cd /d "%~dp0" || exit /b 1

if exist ".venv\Scripts\python.exe" goto dependencies

where py.exe >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv || exit /b 1
        goto dependencies
    )
)

where python.exe >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 (
        python -m venv .venv || exit /b 1
        goto dependencies
    )
)

set "INSTALLED_PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%INSTALLED_PYTHON%" (
    where winget.exe >nul 2>&1
    if errorlevel 1 goto missing_python
    winget install --id Python.Python.3.12 --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
    if errorlevel 1 goto missing_python
)
if not exist "%INSTALLED_PYTHON%" goto missing_python
"%INSTALLED_PYTHON%" -m venv .venv || exit /b 1

:dependencies
".venv\Scripts\python.exe" -m pip install -c requirements.txt mcp pydantic starlette uvicorn httpx || exit /b 1
".venv\Scripts\python.exe" -m tools.local_server configure || exit /b 1
echo Instalacion lista. Para iniciar el servidor: .venv\Scripts\python.exe -m tools.local_server start
exit /b 0

:missing_python
echo Instala Python 3.10 o superior desde https://www.python.org/downloads/windows/ y vuelve a ejecutar el instalador. 1>&2
exit /b 1
