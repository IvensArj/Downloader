@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  Gera o executavel do Baixador de Midia.
rem
rem    build_exe.bat            pasta dist\BaixadorMidia\ e o zip
rem                             dist\BaixadorMidia-windows.zip   (recomendado)
rem    build_exe.bat onefile    arquivo unico dist\BaixadorMidia.exe
rem                             (abre mais devagar e dispara mais alertas de antivirus)
rem
rem  Requer ffmpeg.exe e ffprobe.exe nesta pasta.
rem ---------------------------------------------------------------------------

set "MODE=--onedir"
set "OUT=dist\BaixadorMidia-windows.zip"
if /i "%~1"=="onefile" (
    set "MODE=--onefile"
    set "OUT=dist\BaixadorMidia.exe"
)

python --version >nul 2>nul
if errorlevel 1 goto :sem_python

if not exist "ffmpeg.exe" goto :sem_ffmpeg
if not exist "ffprobe.exe" goto :sem_ffmpeg

echo [1/3] Instalando dependencias...
python -m pip install -r requirements-build.txt
if errorlevel 1 goto :falha

echo [2/3] Gerando o executavel %MODE%...
python -m PyInstaller --noconfirm --clean %MODE% --windowed --name BaixadorMidia ^
  --collect-data customtkinter --collect-all yt_dlp_ejs --collect-all curl_cffi ^
  --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." ^
  downloader.py
if errorlevel 1 goto :falha
if exist "BaixadorMidia.spec" del "BaixadorMidia.spec"

if "%MODE%"=="--onedir" (
    echo [3/3] Compactando a pasta...
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\BaixadorMidia' -DestinationPath '%OUT%' -Force"
    if errorlevel 1 goto :falha
)

echo.
echo Pronto: %OUT%
powershell -NoProfile -Command "'SHA256: ' + (Get-FileHash '%OUT%' -Algorithm SHA256).Hash"
if not defined CI pause
exit /b 0

:sem_python
echo [ERRO] Python nao encontrado no PATH. Instale o Python 3.10 ou superior
echo        e marque a opcao "Add python.exe to PATH".
goto :falha

:sem_ffmpeg
echo [ERRO] Coloque ffmpeg.exe e ffprobe.exe nesta pasta antes de gerar o executavel.
echo        Baixe o arquivo win64-lgpl.zip em https://github.com/BtbN/FFmpeg-Builds/releases
echo        e copie os dois arquivos da pasta bin.
goto :falha

:falha
echo.
echo A geracao falhou. Veja as mensagens acima.
if not defined CI pause
exit /b 1
