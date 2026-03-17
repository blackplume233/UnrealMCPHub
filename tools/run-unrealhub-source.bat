@echo off
setlocal
set "ROOT=C:\Users\alain\Documents\Playground\UnrealMCPHub"
set "PYTHON=%ROOT%\tools\python311\embed\python.exe"
set "WRAPPER=%ROOT%\tools\run_unrealhub_source.py"

if not exist "%PYTHON%" (
  echo Portable Python not found: %PYTHON%
  exit /b 1
)

"%PYTHON%" "%WRAPPER%" %*
