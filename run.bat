@echo off
title Inversiones Inmobiliarias
cd /d "%~dp0"
echo.
echo =====================================================
echo   INVERSIONES INMOBILIARIAS  -  Servidor local
echo =====================================================
echo.

REM ── 1. Verificar Python ─────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python no esta instalado o no esta en el PATH.
    echo Descargalo desde https://www.python.org/downloads/
    echo Asegurate de marcar "Add Python to PATH" durante la instalacion.
    pause
    exit /b 1
)

REM ── 2. Crear entorno virtual si no existe ────────────
if not exist "venv\Scripts\python.exe" (
    echo [1/3] Creando entorno virtual ^(solo la primera vez^)...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR al crear el entorno virtual.
        pause
        exit /b 1
    )
    echo [2/3] Instalando dependencias ^(puede tardar unos minutos^)...
    venv\Scripts\pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo ERROR al instalar dependencias.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Entorno virtual listo.
    echo [2/3] Dependencias OK.
)

REM ── 3. Cargar datos iniciales de propiedades ─────────
echo [3/3] Verificando datos iniciales...
venv\Scripts\python seed_propiedades.py

REM ── 4. Abrir navegador y arrancar servidor ───────────
echo.
echo  Servidor iniciando en: http://localhost:5000
echo  Cerra esta ventana para detener el servidor.
echo =====================================================
echo.

start /min "" cmd /c "timeout /t 3 /nobreak > nul && start http://localhost:5000"

venv\Scripts\python app.py

echo.
echo Servidor detenido.
pause
