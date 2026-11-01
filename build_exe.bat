@echo off
REM Builds dist\SpaceFractionsAgent.exe  (run from the project folder on Windows)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ...
    python -m venv .venv || goto :error
)
call ".venv\Scripts\activate.bat"

echo Installing build tools ...
python -m pip install --quiet -r requirements-dev.txt pyinstaller==6.11.1 || goto :error

echo Running tests ...
python -m pytest -q || goto :error

echo Building the .exe ...
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name SpaceFractionsAgent ^
    --add-data "inputs;inputs" ^
    --hidden-import agent._bundle ^
    sf_agent_app.py || goto :error

echo.
echo SUCCESS: dist\SpaceFractionsAgent.exe
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
