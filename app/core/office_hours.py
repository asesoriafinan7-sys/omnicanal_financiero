"""
office_hours.py — Guard de Horario Laboral y Cola de Mensajes.

Funciones:
  • is_office_hours()    → True si estamos en horario de atención.
  • enqueue_message()    → Guarda mensaje en Firestore cola.
  • process_queue()      → Procesa la cola al inicio de la jornada.
  • get_mensaje_espera() → Texto del mensaje de fuera de horario.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")
except Exception:
    from datetime import timezone, timedelta
    # Fallback a UTC-5 si no hay base de datos de zonas horarias (común en Windows sin tzdata)
    BOGOTA_TZ = timezone(timedelta(hours=-5))

logger = logging.getLogger(__name__)

# ─── Festivos Colombia 2025-2026 (fecha ISO) ─────────────────────────────────
# Actualizar anualmente. Se cargan también desde business_rules.json si están disponibles.
_FESTIVOS_CO: set[str] = {
    # 2025
    "2025-01-01", "2025-01-06", "2025-03-24", "2025-04-17", "2025-04-18",
    "2025-05-01", "2025-06-02", "2025-06-23", "2025-06-30", "2025-07-20",
    "2025-08-07", "2025-08-18", "2025-10-13", "2025-11-03", "2025-11-17",
    "2025-12-08", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-12", "2026-03-23", "2026-04-02", "2026-04-03",
    "2026-05-01", "2026-05-18", "2026-06-08", "2026-06-29", "2026-07-20",
    "2026-08-07", "2026-08-17", "2026-10-12", "2026-11-02", "2026-11-16",
    "2026-12-08", "2026-12-25",
}


def _load_rules() -> Dict[str, Any]:
    """Carga horario laboral desde business_rules.json con fallback a defaults."""
    import json
    from pathlib import Path

    rules_path = Path(__file__).parent / "business_rules.json"
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            return json.load(f).get("horario_laboral", {})
    except Exception as exc:
        logger.warning("No se pudo cargar business_rules.json: %s — usando defaults.", exc)
        return {}


def is_office_hours(now: Optional[datetime] = None) -> bool:
    """
    Retorna True si el momento actual está dentro del horario laboral colombiano.
    Respeta festivos, sábados (turno reducido) y domingos (cerrado).
    """
    rules = _load_rules()
    if now is None:
        now = datetime.now(BOGOTA_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BOGOTA_TZ)

    fecha_str = now.strftime("%Y-%m-%d")
    dia_semana = now.weekday()  # 0=Lunes … 6=Domingo

    # Festivos siempre cerrado
    if rules.get("respetar_festivos_colombia", True) and fecha_str in _FESTIVOS_CO:
        return False

    # Domingo: siempre cerrado
    if dia_semana == 6:
        return False

    hora_actual = now.time()

    # Sábado
    if dia_semana == 5:
        sab = rules.get("sabado")
        if not sab:
            return False
        inicio = dtime.fromisoformat(sab["inicio"])
        fin    = dtime.fromisoformat(sab["fin"])
        return inicio <= hora_actual < fin

    # Lunes a Viernes
    lv = rules.get("lunes_viernes", {"inicio": "08:00", "fin": "18:00"})
    inicio = dtime.fromisoformat(lv["inicio"])
    fin    = dtime.fromisoformat(lv["fin"])
    return inicio <= hora_actual < fin


def get_mensaje_espera() -> str:
    """Retorna el mensaje de fuera de horario configurado en las reglas de negocio."""
    rules = _load_rules()
    return rules.get(
        "mensaje_fuera_horario",
        "¡Hola! 👋 Estamos fuera de horario. Tu mensaje fue recibido y un asesor te contactará al inicio de la próxima jornada. 🏦",
    )


# ─── Cola de mensajes en Firestore ───────────────────────────────────────────

def _get_db():
    """Importación lazy de Firestore para evitar inicio lento en dev/test."""
    from google.cloud import firestore
    from app.core.config import get_settings
    return firestore.Client(project=get_settings().google_cloud_project)


def enqueue_message(
    telefono: str,
    texto: str,
    nombre: str = "Desconocido",
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Guarda un mensaje en la cola de Firestore para procesamiento diferido.
    Retorna el ID del documento creado.
    """
    db = _get_db()
    from google.cloud import firestore

    doc_data: Dict[str, Any] = {
        "telefono":  telefono,
        "texto":     texto,
        "nombre":    nombre,
        "recibido_en": firestore.SERVER_TIMESTAMP,
        "procesado": False,
        "extra":     extra or {},
    }
    _, doc_ref = db.collection("messages_queue").add(doc_data)
    logger.info("Mensaje de %s encolado (fuera de horario) → doc_id: %s", telefono, doc_ref.id)
    return doc_ref.id


async def process_queue(max_items: int = 100) -> int:
    """
    Procesa los mensajes pendientes en la cola de manera asíncrona.
    Aplica un rate-limit de 4 segundos entre envíos para seguridad anti-ban.
    """
    if not is_office_hours():
        logger.info("process_queue: fuera de horario — no se procesa cola.")
        return 0

    db = _get_db()
    from google.cloud import firestore
    import asyncio

    cola = (
        db.collection("messages_queue")
        .where("procesado", "==", False)
        .order_by("recibido_en")
        .limit(max_items)
        .stream()
    )

    procesados = 0
    for doc in cola:
        try:
            data = doc.to_dict()
            # Inyectar al pipeline de forma asíncrona y esperar delay
            await _reenviar_a_pipeline(data)
            
            doc.reference.update({
                "procesado": True,
                "procesado_en": firestore.SERVER_TIMESTAMP,
            })
            procesados += 1
            
            # Rate limit preventivo entre mensajes de la cola
            if procesados < max_items:
                logger.info("Esperando 4s para el siguiente mensaje de la cola...")
                await asyncio.sleep(4.0)
                
        except Exception as exc:
            logger.error("Error procesando mensaje encolado %s: %s", doc.id, exc)

    logger.info("Cola procesada: %d mensajes reactivados con contexto temporal.", procesados)
    return procesados


async def _reenviar_a_pipeline(data: Dict[str, Any]) -> None:
    """
    Reenvia un mensaje encolado al pipeline con flag de reactivación.
    """
    from app.routers.api import _procesar_mensaje_entrante
    from datetime import datetime

    # Payload sintético con metadato de reactivación
    body = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "queue_reactivation",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "is_queue_reactivation": True,
                        "original_received_at": data.get("recibido_en").isoformat() if hasattr(data.get("recibido_en"), "isoformat") else None
                    },
                    "contacts": [{"profile": {"name": data.get("nombre", "Desconocido")}, "wa_id": data["telefono"].replace("+", "")}],
                    "messages": [{
                        "from": data["telefono"].replace("+", ""),
                        "id": f"q_{data['telefono']}_{int(datetime.now().timestamp())}",
                        "timestamp": str(int(datetime.now().timestamp())),
                        "text": {"body": data["texto"]},
                        "type": "text",
                    }],
                },
            }],
        }],
    }

    try:
        await _procesar_mensaje_entrante(body)
    except Exception as exc:
        logger.error("Error reenviando mensaje desde cola: %s", exc)
