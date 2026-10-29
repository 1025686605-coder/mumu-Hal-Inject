@echo off
rem MuMu running-data injector - single-file launcher
setlocal
set "HERE=%~dp0"

if exist "%HERE%dist\mumu_runner.pyz" (
    where pythonw >nul 2>&1 && (start "" pythonw "%HERE%dist\mumu_runner.pyz" & exit /b 0)
    where python  >nul 2>&1 && (start "" python  "%HERE%dist\mumu_runner.pyz" & exit /b 0)
)

if exist "%HERE%app.py" (
    where pythonw >nul 2>&1 && (start "" pythonw "%HERE%app.py" & exit /b 0)
    where python  >nul 2>&1 && (start "" python  "%HERE%app.py" & exit /b 0)
)

echo Python not found. Install Python 3 first.
pause
exit /b 1
