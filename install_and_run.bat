@echo off
:: ══════════════════════════════════════════════════════════════════════════════
:: install_and_run.bat — Instalador y Lanzador del Ecosistema Omnicanal v3.0
:: Doble clic para instalar y arrancar el servidor FastAPI localmente.
:: ══════════════════════════════════════════════════════════════════════════════

title Ecosistema Omnicanal Financiero v3.0 — Instalador
color 0A
cls

echo ============================================================
echo   ECOSISTEMA OMNICANAL FINANCIERO v3.0
echo   Instalador y Lanzador Local — Windows
echo ============================================================
echo.

:: ── Verificar Python ──────────────────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Python no encontrado en el PATH.
    echo Descarga Python 3.11+ desde: https://python.org/downloads
    echo Marca "Add Python to PATH" durante la instalacion.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% detectado.

:: ── Verificar version minima de Python (3.9+) ────────────────────────────────
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Se requiere Python 3.9 o superior.
    pause
    exit /b 1
)

:: ── Crear entorno virtual si no existe ───────────────────────────────────────
if not exist ".venv\" (
    echo.
    echo [1/5] Creando entorno virtual...
    python -m venv .venv
    if %errorlevel% neq 0 (
        color 0C
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
    echo [OK] Entorno virtual creado en .venv\
) else (
    echo [OK] Entorno virtual ya existe.
)

:: ── Activar entorno virtual ───────────────────────────────────────────────────
echo.
echo [2/5] Activando entorno virtual...
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)
echo [OK] Entorno virtual activo.

:: ── Actualizar pip ────────────────────────────────────────────────────────────
echo.
echo [3/5] Actualizando pip...
python -m pip install --upgrade pip --quiet

:: ── Instalar dependencias ─────────────────────────────────────────────────────
echo.
echo [4/5] Instalando dependencias (puede tomar varios minutos)...
if not exist "requirements.txt" (
    color 0C
    echo [ERROR] No se encontro requirements.txt
    pause
    exit /b 1
)
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Fallo la instalacion de dependencias.
    echo Revisa el error arriba y ejecuta manualmente:
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas.

:: ── Verificar existencia de .env ─────────────────────────────────────────────
echo.
echo [5/5] Verificando configuracion...
if not exist ".env" (
    echo.
    echo [AVISO] No se encontro el archivo .env
    echo Copiando plantilla desde .env.example...
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [OK] .env creado. Editalo con tus credenciales reales antes de continuar.
        echo.
        echo Abre .env con el Bloc de Notas y completa:
        echo   - GOOGLE_CLOUD_PROJECT
        echo   - META_ACCESS_TOKEN y META_PHONE_NUMBER_ID
        echo   - SMTP_PASS (Contrasena de Aplicacion Gmail)
        echo   - TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
        echo   - GSHEETS_SPREADSHEET_ID
        echo   - WICAPITAL_USER y WICAPITAL_PASS
        echo.
        echo Presiona cualquier tecla cuando hayas guardado el .env para continuar...
        notepad .env
        pause >nul
    ) else (
        echo [AVISO] No se encontro .env.example. Ejecutando sin variables de entorno.
    )
) else (
    echo [OK] Archivo .env encontrado.
)

:: ── Crear carpeta de logs ─────────────────────────────────────────────────────
if not exist "logs\" mkdir logs
if not exist "bases\" mkdir bases
if not exist "chats\" mkdir chats

:: ── Verificacion rapida de importaciones ──────────────────────────────────────
echo.
echo Verificando que el codigo puede importarse correctamente...
python -c "from app.core.config import get_settings; from app.core.business_rules import get_rules_engine; print('[OK] Modulos core cargados.')"
if %errorlevel% neq 0 (
    color 0E
    echo [AVISO] Algunos modulos no se pudieron importar.
    echo Esto puede ser normal si faltan credenciales de GCP.
    echo El servidor intentara iniciar de todas formas.
)

:: ── Lanzar servidor FastAPI ───────────────────────────────────────────────────
echo.
echo ============================================================
echo   Iniciando servidor FastAPI en http://localhost:8080
echo   Documentacion : http://localhost:8080/docs
echo   Para detener  : Ctrl+C
echo ============================================================
echo.

set APP_ENV=development
python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload --log-level info

:: ── Si el servidor se detiene ─────────────────────────────────────────────────
echo.
echo [INFO] El servidor se ha detenido.
pause
