"""
logging_config.py — Configuración de logging avanzado con archivos rotativos diarios.

Características:
  • Rotación diaria automática con retención de 30 días.
  • Formato JSON estructurado para Cloud Logging en producción.
  • Formato legible para consola en desarrollo.
  • Compatible con uvicorn y FastAPI.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


LOG_DIR = Path("logs")
LOG_LEVEL_PROD = logging.INFO
LOG_LEVEL_DEV  = logging.DEBUG


class _JsonFormatter(logging.Formatter):
    """Formateador JSON para Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime, timezone

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _DevFormatter(logging.Formatter):
    """Formateador legible con colores para consola de desarrollo."""

    _COLORS = {
        "DEBUG":    "\033[36m",   # Cyan
        "INFO":     "\033[32m",   # Green
        "WARNING":  "\033[33m",   # Yellow
        "ERROR":    "\033[31m",   # Red
        "CRITICAL": "\033[41m",   # Red background
        "RESET":    "\033[0m",
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        reset = self._COLORS["RESET"]
        record.levelname = f"{color}{record.levelname:<8}{reset}"
        return super().format(record)


def configure_logging(app_env: str = "development") -> None:
    """
    Configura el sistema de logging global.
    Llamar una sola vez al inicio de la aplicación.
    """
    is_prod = app_env == "production"
    level   = LOG_LEVEL_PROD if is_prod else LOG_LEVEL_DEV

    # Limpiar handlers previos del root logger
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    # ── Handler de consola ───────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    if is_prod:
        console_handler.setFormatter(_JsonFormatter())
    else:
        fmt = _DevFormatter(
            fmt="%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s",
            datefmt="%H:%M:%S",
        )
        console_handler.setFormatter(fmt)

    root.addHandler(console_handler)

    # ── Handler de archivo rotativo diario ──────────────────────────────────
    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=LOG_DIR / "omnicanal.log",
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)
    except PermissionError:
        # En Cloud Run el filesystem es read-only fuera de /tmp
        tmp_handler = logging.handlers.TimedRotatingFileHandler(
            filename="/tmp/omnicanal.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
            utc=True,
        )
        tmp_handler.setFormatter(_JsonFormatter())
        root.addHandler(tmp_handler)

    # ── Silenciar loggers muy verbosos ───────────────────────────────────────
    for noisy in ("selenium", "urllib3", "httpcore", "httpx", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configurado [entorno=%s, nivel=%s]", app_env, logging.getLevelName(level)
    )
