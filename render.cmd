@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m image_render.render %*
exit /b %errorlevel%
