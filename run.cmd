@echo off
setlocal
cd /d "%~dp0"
set "BOYO_LOCAL=%~dp0..\local"
for %%I in ("%BOYO_LOCAL%") do set "BOYO_LOCAL=%%~fI"
set "PYTHONPYCACHEPREFIX=%BOYO_LOCAL%\cache\pycache"
set "TEMP=%BOYO_LOCAL%\tmp"
set "TMP=%TEMP%"
if not exist "%BOYO_LOCAL%\tmp" mkdir "%BOYO_LOCAL%\tmp"
if exist "%BOYO_LOCAL%\python\python.exe" (
  "%BOYO_LOCAL%\python\python.exe" "%~dp0portable_env.py"
  if errorlevel 1 (
    echo Environment repair failed. See the message above.
    pause
    exit /b 1
  )
)
if not exist "%BOYO_LOCAL%\environment\Scripts\pythonw.exe" (
  echo Local environment is missing. Run the setup PowerShell script in this folder.
  pause
  exit /b 1
)
start "Boyo" "%BOYO_LOCAL%\environment\Scripts\pythonw.exe" "%~dp0launcher.py"
exit /b 0
