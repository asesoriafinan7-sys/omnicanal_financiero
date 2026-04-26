"""
main.py v3.0 — Punto de entrada FastAPI.

CAMBIOS v3.0:
  • Arquitectura Stateless & Reactive.
  • Orquestación movida a GCP Cloud Scheduler.
  • Alerta Telegram en startup/shutdown.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.core.websockets import manager

_settings = get_settings()
configure_logging(_settings.app_env)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestión del ciclo de vida de la aplicación."""
    logger.info("=" * 60)
    logger.info("  Ecosistema Omnicanal Financiero v3.0 — Iniciando")
    logger.info("  Entorno : %s", _settings.app_env)
    logger.info("  Proyecto: %s", _settings.google_cloud_project)
    logger.info("  Modo    : Stateless & Reactive (Cloud Scheduler enabled)")
    logger.info("=" * 60)

    # Notificar inicio a Telegram
    try:
        from app.services.crm_sync import TelegramAlerter
        TelegramAlerter().send(
            f"🚀 <b>Ecosistema Omnicanal v3.0 iniciado</b>\n"
            f"🌐 Entorno: <code>{_settings.app_env}</code>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception as e:
        logger.warning("No se pudo notificar inicio a Telegram: %s", e)

    yield  # ← Aplicación corriendo

    # Shutdown
    logger.info("Ecosistema Omnicanal v3.0 — Apagado limpio.")
    try:
        from app.services.crm_sync import TelegramAlerter
        TelegramAlerter().send("🔴 <b>Ecosistema Omnicanal v3.0</b> — Servidor apagado.")
    except Exception:
        pass


app = FastAPI(
    title="Ecosistema Omnicanal Financiero",
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS
_allowed_origins = (
    ["*"]
    if _settings.app_env == "development"
    else [
        "https://asesoriafinan7.com",
        "https://asesoriafinan7.com.co",
        "https://*.run.app",
    ]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Excepción no manejada en %s: %s", request.url, exc, exc_info=True)
    try:
        from app.services.crm_sync import TelegramAlerter
        TelegramAlerter().alerta_error_critico(
            f"Endpoint {request.method} {request.url.path}", str(exc)[:400]
        )
    except Exception:
        pass
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno.", "error": str(exc)[:200]},
    )


# Routers
from app.routers.api import router as api_router
app.include_router(api_router, prefix="/api/v1")


# Dashboard / Static
import os
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("app/static/index.html")

@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
