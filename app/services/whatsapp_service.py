"""
whatsapp_service.py — Servicio de WhatsApp Cloud API para envíos y notificaciones.

Cubre:
  • Envío de mensajes de texto libre.
  • Envío de plantillas preaprobadas (HSM) para retoma de clientes históricos.
  • Motor de envío masivo para campañas (con control de rate-limit).
  • Análisis y extracción de chats exportados (extractor_wsp refactorizado).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import requests

from app.core.config import get_settings
from app.models.schemas import AuditoriaConversacion

logger = logging.getLogger(__name__)
_settings = get_settings()

# ─── Plantillas preaprobadas disponibles ─────────────────────────────────────
# Las claves son identificadores internos; los valores son los nombres exactos
# tal como están aprobados en el Business Manager de Meta.
PLANTILLAS = {
    "RETOMA_LIBRANZA": "retoma_libranza_v2",
    "RETOMA_CONSUMO": "retoma_consumo_v2",
    "RETOMA_COMPRA_CARTERA": "retoma_compra_cartera_v1",
    "BIENVENIDA": "bienvenida_prospecto_v1",
    "DOCUMENTOS_PENDIENTES": "solicitud_documentos_v3",
    "APROBACION_CREDITO": "aprobacion_credito_v1",
    "DESEMBOLSO_LISTO": "desembolso_listo_v1",
}


class WhatsAppCloudAPI:
    """
    Motor de envíos y notificaciones conectado a Meta Graph API.
    Soporta mensajes libres, plantillas HSM y envíos masivos con rate-limiting.
    """

    RATE_LIMIT_DELAY: float = 1.2  # segundos entre envíos masivos

    def __init__(self) -> None:
        self._token = _settings.meta_access_token
        self._phone_number_id = _settings.meta_phone_number_id
        if not self._token or not self._phone_number_id:
            raise ValueError("META_ACCESS_TOKEN y META_PHONE_NUMBER_ID son requeridos.")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Envía solicitud POST a la API de WhatsApp Cloud."""
        resp = requests.post(
            _settings.whatsapp_api_url,
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Mensaje de texto libre ─────────────────────────────────────────────

    def enviar_texto(self, telefono: str, mensaje: str) -> Dict[str, Any]:
        """Envía un mensaje de texto libre. Solo para conversaciones dentro de ventana 24h."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje},
        }
        try:
            resp = self._post(payload)
            logger.info("Texto enviado a %s — MsgID: %s", telefono, resp.get("messages", [{}])[0].get("id"))
            return {"exito": True, "response": resp}
        except requests.HTTPError as exc:
            logger.error("Error enviando texto a %s: %s", telefono, exc.response.text)
            return {"exito": False, "error": exc.response.text}
        except Exception as exc:
            logger.error("Error inesperado enviando texto: %s", exc)
            return {"exito": False, "error": str(exc)}

    # ── Plantilla HSM (fuera de ventana 24h o para campañas) ──────────────

    def enviar_plantilla(
        self,
        telefono: str,
        nombre_plantilla: str,
        idioma: str = "es",
        componentes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Envía una plantilla preaprobada (HSM).
        `componentes` permite personalizar variables {{1}}, {{2}}, etc.
        """
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "template",
            "template": {
                "name": nombre_plantilla,
                "language": {"code": idioma},
            },
        }
        if componentes:
            payload["template"]["components"] = componentes

        try:
            resp = self._post(payload)
            msg_id = resp.get("messages", [{}])[0].get("id", "N/A")
            logger.info("Plantilla '%s' enviada a %s — MsgID: %s", nombre_plantilla, telefono, msg_id)
            return {"exito": True, "msg_id": msg_id, "response": resp}
        except requests.HTTPError as exc:
            logger.error("Error plantilla '%s' a %s: %s", nombre_plantilla, telefono, exc.response.text)
            return {"exito": False, "error": exc.response.text}
        except Exception as exc:
            logger.error("Error inesperado en plantilla: %s", exc)
            return {"exito": False, "error": str(exc)}

    # ── Retoma de clientes históricos ─────────────────────────────────────

    def retomar_cliente(
        self,
        telefono: str,
        nombre_cliente: str,
        producto: str = "LIBRANZA",
        tasa_oferta: str = "1.3",
    ) -> Dict[str, Any]:
        """
        Envía plantilla de retoma de cliente histórico con variables personalizadas.
        Mapea automáticamente el producto a la plantilla correcta.
        """
        clave_plantilla = f"RETOMA_{producto.upper()}"
        nombre_plantilla = PLANTILLAS.get(clave_plantilla, PLANTILLAS["RETOMA_LIBRANZA"])

        componentes = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre_cliente},
                    {"type": "text", "text": tasa_oferta},
                    {"type": "text", "text": producto.title()},
                ],
            }
        ]
        return self.enviar_plantilla(telefono, nombre_plantilla, componentes=componentes)

    # ── Envío masivo con control de rate-limit ────────────────────────────

    def campana_masiva(
        self,
        lista_contactos: List[Dict[str, str]],
        nombre_plantilla_interna: str,
        lote_size: int = 50,
    ) -> Dict[str, Any]:
        """
        Envío masivo de campañas usando plantillas preaprobadas.
        lista_contactos: [{"telefono": "+57...", "nombre": "...", "tasa": "...", "producto": "..."}]
        lote_size: envíos por lote antes de pausar 5 segundos.
        """
        nombre_plantilla = PLANTILLAS.get(nombre_plantilla_interna, nombre_plantilla_interna)
        enviados = 0
        fallidos = 0
        errores: List[str] = []

        for idx, contacto in enumerate(lista_contactos):
            telefono = contacto.get("telefono", "")
            nombre = contacto.get("nombre", "Cliente")
            tasa = contacto.get("tasa", "1.3")
            producto = contacto.get("producto", "Libranza")

            componentes = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre},
                        {"type": "text", "text": tasa},
                        {"type": "text", "text": producto},
                    ],
                }
            ]
            resultado = self.enviar_plantilla(telefono, nombre_plantilla, componentes=componentes)

            if resultado.get("exito"):
                enviados += 1
            else:
                fallidos += 1
                errores.append(f"{telefono}: {resultado.get('error', 'Unknown')}")

            # Rate-limiting: pausa cada N envíos
            time.sleep(self.RATE_LIMIT_DELAY)
            if (idx + 1) % lote_size == 0:
                logger.info("Lote %d completado. Pausa de 5 segundos...", (idx + 1) // lote_size)
                time.sleep(5)

        logger.info("Campaña masiva finalizada: %d enviados, %d fallidos.", enviados, fallidos)
        return {
            "total_contactos": len(lista_contactos),
            "enviados": enviados,
            "fallidos": fallidos,
            "errores": errores[:20],  # Máx 20 errores en respuesta
        }

    # ── Marcar mensaje como leído ──────────────────────────────────────────

    def marcar_leido(self, message_id: str) -> bool:
        """Marca un mensaje entrante como leído (doble check azul)."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            self._post(payload)
            return True
        except Exception as exc:
            logger.warning("No se pudo marcar como leído %s: %s", message_id, exc)
            return False


# ═════════════════════════════════════════════════════════════════════════════
# MOTOR HSM ASÍNCRONO (para motor_reactivacion.py y BackgroundTasks)
# Usa httpx.AsyncClient en lugar de requests para no bloquear el event loop
# ═════════════════════════════════════════════════════════════════════════════

async def enviar_plantilla_hsm(
    numero_destino: str,
    nombre_plantilla: str,
    variables: Optional[List[str]] = None,
    idioma: str = "es_CO",
) -> Dict[str, Any]:
    """
    Envía una Plantilla HSM aprobada por Meta de forma completamente asíncrona.

    Diseñado para:  motor_reactivacion.py, BackgroundTasks de FastAPI.
    Principio SOLID: Función de responsabilidad única — solo envía HSMs.

    Args:
        numero_destino:    Teléfono en formato internacional (+573XXXXXXXXX).
        nombre_plantilla:  Nombre exacto de la plantilla en Meta Business Manager.
        variables:         Lista de variables dinámicas en orden ({{1}}, {{2}}, ...).
        idioma:            Código de idioma de la plantilla (default: es_CO).

    Returns:
        dict con 'exito', 'msg_id', y 'error' en caso de fallo.
    """
    # ── Construcción del payload base ──
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "template",
        "template": {
            "name": nombre_plantilla,
            "language": {"code": idioma},
        },
    }

    # ── Inyección de variables dinámicas al body ──
    if variables:
        parametros = [{"type": "text", "text": str(var)} for var in variables]
        payload["template"]["components"] = [
            {
                "type": "body",
                "parameters": parametros,
            }
        ]

    # ── Endpoint y headers (secretos desde entorno) ──
    url = (
        f"https://graph.facebook.com/v18.0/"
        f"{_settings.meta_phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {_settings.meta_access_token}",
        "Content-Type": "application/json",
    }

    # ── POST asíncrono con diagnóstico detallado de fallos de Meta ──
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)

        if response.status_code == 200:
            data = response.json()
            msg_id = data.get("messages", [{}])[0].get("id", "N/A")
            logger.info(
                "[HSM_OK] plantilla='%s' | destino=%s | msg_id=%s",
                nombre_plantilla, numero_destino, msg_id,
            )
            return {"exito": True, "msg_id": msg_id, "response": data}
        else:
            # Diagnóstico completo del rechazo de Meta
            logger.error(
                "[HSM_ERROR] plantilla='%s' | destino=%s | status=%s | body=%s",
                nombre_plantilla, numero_destino,
                response.status_code, response.text,
            )
            return {
                "exito": False,
                "status_code": response.status_code,
                "error": response.text,
            }

    except httpx.TimeoutException:
        logger.error(
            "[HSM_TIMEOUT] plantilla='%s' | destino=%s | Meta no respondió en 15s",
            nombre_plantilla, numero_destino,
        )
        return {"exito": False, "error": "timeout"}

    except Exception as exc:
        logger.critical(
            "[HSM_CRITICAL] plantilla='%s' | destino=%s | exc=%s",
            nombre_plantilla, numero_destino, str(exc),
        )
        return {"exito": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTOR Y AUDITOR DE CHATS WSP (extractor_wsp.py refactorizado)
# ═══════════════════════════════════════════════════════════════════════════════

# Patrón para líneas de chat exportado de WhatsApp
_PATTERN_MENSAJE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?\s?m\.?)?)\s*[-–]\s*(.+?):\s*(.+)$",
    re.IGNORECASE,
)

# Palabras clave de objeciones para pre-análisis local
_OBJECIONES_KEYWORDS: Dict[str, List[str]] = {
    "OBJECION_TASA": [
        "tasa", "interés", "interes", "porcentaje", "muy cara", "muy alto",
        "cobran mucho", "es caro", "prestamo caro",
    ],
    "OBJECION_CAPACIDAD_ENDEUDAMIENTO": [
        "no me alcanza", "cuota alta", "capacidad", "endeudamiento",
        "ya tengo crédito", "sobreendeudado", "descuento alto",
    ],
    "OBJECION_DOCUMENTACION": [
        "documentos", "papeles", "certificado", "extracto", "desprendible",
        "no tengo", "sin documentos", "tramite",
    ],
    "OBJECION_TIEMPO": [
        "mucho tiempo", "demora", "cuánto tarda", "cuando aprueban",
        "muy lento", "urgente",
    ],
    "OBJECION_COMPETENCIA": [
        "banco", "fincomercio", "liberate", "otra entidad", "comparar",
        "me ofrecen menos", "ya me aprobaron",
    ],
}


def analizar_chat_exportado_local(chat_texto: str) -> Dict[str, Any]:
    """
    Análisis local rápido (sin IA) de un chat exportado de WhatsApp.
    Detecta objeciones por palabras clave, cuenta mensajes y participantes.
    Complementa el análisis profundo de Llama 3.3.
    """
    mensajes: List[Dict[str, str]] = []
    participantes: set = set()
    fechas: List[str] = []

    for linea in chat_texto.splitlines():
        match = _PATTERN_MENSAJE.match(linea.strip())
        if match:
            fecha_str = match.group(1)
            hora_str = match.group(2)
            remitente = match.group(3).strip()
            texto = match.group(4).strip()

            mensajes.append({
                "fecha": fecha_str,
                "hora": hora_str,
                "remitente": remitente,
                "texto": texto,
            })
            participantes.add(remitente)
            fechas.append(fecha_str)

    # Detectar objeciones por keywords
    objeciones_detectadas: set = set()
    texto_completo = " ".join(m["texto"].lower() for m in mensajes)
    for objecion, keywords in _OBJECIONES_KEYWORDS.items():
        if any(kw in texto_completo for kw in keywords):
            objeciones_detectadas.add(objecion)

    return {
        "total_mensajes": len(mensajes),
        "participantes": list(participantes),
        "primer_mensaje_fecha": fechas[0] if fechas else None,
        "ultimo_mensaje_fecha": fechas[-1] if fechas else None,
        "objeciones_detectadas_local": list(objeciones_detectadas),
        "fragmento_para_ia": "\n".join(
            f"[{m['remitente']}]: {m['texto']}" for m in mensajes[:150]
        ),  # Primeros 150 mensajes para Llama
    }


def procesar_csv_contactos(csv_contenido: str) -> List[Dict[str, str]]:
    """
    Procesa un CSV de contactos y retorna lista normalizada.
    Compatible con exports de Google Contacts, Excel y bases propias.
    """
    reader = csv.DictReader(io.StringIO(csv_contenido))
    contactos: List[Dict[str, str]] = []

    campo_telefono_aliases = ["telefono", "celular", "phone", "movil", "tel", "mobile"]
    campo_nombre_aliases = ["nombre", "name", "nombres", "nombre_completo", "full_name"]

    for row in reader:
        row_lower = {k.lower().strip(): v for k, v in row.items()}

        telefono = ""
        for alias in campo_telefono_aliases:
            val = row_lower.get(alias, "").strip()
            if val:
                telefono = val
                break

        nombre = "Desconocido"
        for alias in campo_nombre_aliases:
            val = row_lower.get(alias, "").strip().title()
            if val:
                nombre = val
                break

        if not telefono:
            continue

        # Normalizar teléfono
        digitos = re.sub(r"\D", "", telefono)
        if len(digitos) == 10 and digitos[0] == "3":
            telefono = f"+57{digitos}"
        elif len(digitos) == 12 and digitos.startswith("57"):
            telefono = f"+{digitos}"

        contactos.append({
            "telefono": telefono,
            "nombre": nombre,
            "email": row_lower.get("email", row_lower.get("correo", "")),
            "sector": row_lower.get("sector", "DESCONOCIDO"),
            "tasa": row_lower.get("tasa", "1.3"),
            "producto": row_lower.get("producto", "LIBRANZA"),
        })

    logger.info("CSV procesado: %d contactos válidos.", len(contactos))
    return contactos
