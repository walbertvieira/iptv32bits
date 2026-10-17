@echo off
rem Gera o IPTV Player 32 bits PORTATIL: pasta dist\IPTV Player com o .exe e o VLC embutido.
rem Funciona em qualquer Windows 10/11 (32 ou 64 bits). Nao precisa de VLC nem Python no PC de destino.
setlocal EnableExtensions
cd /d "%~dp0"
call :findpy
if not defined PY goto :nopy
echo Usando Python: %PY%
if not exist ".venv32\Scripts\python.exe" %PY% -m venv .venv32
if not exist ".venv32\Scripts\python.exe" goto :fail
set "VPY=.venv32\Scripts\python.exe"
echo [1/5] Instalando dependencias...
"%VPY%" -m pip install -q --disable-pip-version-check -r requirements.txt "pyinstaller>=6.10,<7"
if errorlevel 1 goto :fail
echo [2/5] Preparando o VLC 32 bits embutido...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0preparar_vlc.ps1" -Arch win32
if errorlevel 1 goto :fail
echo [3/5] Gerando o executavel...
"%VPY%" -m PyInstaller --noconfirm --clean --windowed --name "IPTV Player" main.py
if errorlevel 1 goto :fail
echo [4/5] Copiando o VLC para dentro do programa...
xcopy /E /I /Y /Q vlc "dist\IPTV Player\vlc" >nul
if errorlevel 1 goto :fail
copy /Y LEIA-ME.md "dist\IPTV Player\" >nul
copy /Y exemplo.m3u "dist\IPTV Player\" >nul
echo [5/5] Testando o programa gerado...
if exist selftest.log del /q selftest.log
start "" /wait "dist\IPTV Player\IPTV Player.exe" --selftest "%CD%\selftest.log"
set "RC=%ERRORLEVEL%"
if exist selftest.log type selftest.log
if not "%RC%"=="0" goto :testfail
echo.
echo PRONTO: dist\IPTV Player\  - copie essa pasta inteira para o PC 32 bits.
pause
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
echo Pode instalar no proprio PC 32 bits ou em um PC 64 bits: o resultado e o mesmo.
pause
exit /b 1

:testfail
echo.
echo O AUTOTESTE FALHOU - envie o conteudo de selftest.log acima para correcao.
pause
exit /b 1

:fail
echo.
echo Algo deu errado. Veja as mensagens acima.
pause
exit /b 1
