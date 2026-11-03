@echo off
setlocal
set PYTHONUTF8=1
"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" "%~dp0render.py" %*
exit /b %errorlevel%
