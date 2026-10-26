@echo off
setlocal
cd /d "%~dp0"
for %%I in ("%~dp0..\local") do set "BOYO_LOCAL=%%~fI"
set "PYTHONPYCACHEPREFIX=%BOYO_LOCAL%\cache\pycache"
set "TEMP=%BOYO_LOCAL%\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
if not exist "%BOYO_LOCAL%\python\python.exe" (
  echo Portable Python is missing. Run the setup PowerShell script first.
  exit /b 1
)
"%BOYO_LOCAL%\python\python.exe" "%~dp0portable_env.py" %*
exit /b %errorlevel%
