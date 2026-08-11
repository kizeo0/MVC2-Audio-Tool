@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   MVC2 Audio Tool - compilador a .exe
echo ============================================
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo ERROR: no se encontro "py" ^(el lanzador de Python^) en el PATH.
    echo Instala Python 3.12 desde https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Instalando/actualizando PyInstaller y Pillow...
py -3.12 -m pip install --upgrade pyinstaller pillow >nul
if errorlevel 1 (
    echo ERROR instalando dependencias. Revisa tu conexion o el PATH de Python.
    pause
    exit /b 1
)

if not exist "DTPKDump.py" (
    echo ERROR: falta DTPKDump.py en esta carpeta.
    pause
    exit /b 1
)
if not exist "ffmpeg.exe" (
    echo ERROR: falta ffmpeg.exe en esta carpeta.
    pause
    exit /b 1
)
if not exist "app.ico" (
    echo ERROR: falta app.ico en esta carpeta.
    pause
    exit /b 1
)
if not exist "assets" (
    echo ERROR: falta la carpeta assets en esta carpeta.
    pause
    exit /b 1
)

echo.
echo Limpiando compilaciones anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Compilando (puede tardar 1-3 minutos)...
py -3.12 -m PyInstaller MVC2_AudioTool.spec --noconfirm

echo.
if exist "dist\MVC2 Audio Tool.exe" (
    echo ============================================
    echo   LISTO: dist\MVC2 Audio Tool.exe
    echo ============================================
) else (
    echo Algo fallo. Revisa el texto de arriba para ver el error.
)
pause
