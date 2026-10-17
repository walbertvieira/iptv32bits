@echo off
rem Roda o IPTV Player (edicao 32 bits) pelo codigo-fonte.
rem Requer o Python 3.12 32 bits (python.org). O VLC e baixado automaticamente para a pasta vlc.
setlocal EnableExtensions
cd /d "%~dp0"
call :findpy
if not defined PY goto :nopy
echo Usando Python: %PY%
if not exist ".venv32\Scripts\python.exe" %PY% -m venv .venv32
if not exist ".venv32\Scripts\python.exe" goto :fail
echo Verificando dependencias...
".venv32\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0preparar_vlc.ps1" -Arch win32
if errorlevel 1 goto :fail
start "" ".venv32\Scripts\pythonw.exe" main.py %*
exit /b 0

:findpy
set "PY="
for %%V in (3.12-32 3.13-32 3.11-32 3.10-32) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
exit /b 0

:nopy
echo.
echo Python 32 bits nao encontrado.
echo Instale o Python 3.12 - Windows installer 32-bit - em https://www.python.org/downloads/windows/
echo e deixe marcadas as opcoes "py launcher" e "Add python.exe to PATH".
pause
exit /b 1

:fail
echo.
echo Algo deu errado. Veja as mensagens acima.
pause
exit /b 1
