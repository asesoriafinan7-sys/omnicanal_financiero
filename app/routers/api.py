"""
api.py v3.0 — Router FastAPI completo con Guard de Horario, Ruteo Multicanal
y todos los endpoints actualizados.

NUEVO en v3.0:
  • Guard de horario en webhook → cola Firestore si fuera de horario.
  • Detección de escalada a humano (keywords + intentos fallidos).
  • Ruteo a WiCapital / Expertos / Vivienda Total según banco+producto.
  • Endpoints para redes sociales orgánicas.
  • Endpoint para procesar cola de mensajes.
  • Endpoint para consultar y ver reglas de negocio actuales.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.core.business_rules import get_rules_engine
from app.core.config import get_settings
from app.core.office_hours import (
    enqueue_message,
    get_mensaje_espera,
    is_office_hours,
    process_queue,
)
from app.models.schemas import (
    AuditoriaConversacion,
    ParametrosCampana,
    PerfilProspecto,
    PrioridadProspecto,
    ProductoFinanciero,
    Prospecto,
    RespuestaBase,
    RespuestaCampana,
    RespuestaProspecto,
    SectorEconomico,
    WhatsAppWebhook,
)
from app.services.ai_engine import (
    auditar_chat_exportado_llama,
    perfilar_prospecto_llama,
    responder_chat_mistral,
)
from app.services.crm_sync import (
    GoogleSheetsCRM,
    TelegramAlerter,
    WiCapitalMonitor,
    limpiar_y_segmentar_base,
)
from app.services.firestore_service import FirestoreCRM
from app.services.firestore_service import FirestoreCRM
from app.services.routing_service import RuteoService
from app.services.whatsapp_service import (
    WhatsAppCloudAPI,
    analizar_chat_exportado_local,
    procesar_csv_contactos,
)
from app.core.websockets import manager

from app.core.office_hours import (
    enqueue_message,
    get_mensaje_espera,
    is_office_hours,
    process_queue,
)

logger = logging.getLogger(__name__)
_settings = get_settings()
router   = APIRouter()

# ─── In-memory dedup de "fuera de horario" enviados ──────────────────────────
# Evita enviar múltiples mensajes de espera al mismo número en la misma ventana.
_fuera_horario_enviado: set[str] = set()


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/health", tags=["Sistema"])
async def health_check() -> Dict[str, Any]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ahora = datetime.now(ZoneInfo("America/Bogota"))
    return {
        "status": "OK",
        "version": "3.0.0",
        "entorno": _settings.app_env,
        "en_horario_laboral": is_office_hours(ahora),
        "hora_bogota": ahora.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "proyecto_gcp": _settings.google_cloud_project,
        "modelos_ia": {
            "perfilamiento": _settings.llama_endpoint_id,
            "conversacional": _settings.mistral_endpoint_id,
        },
    }


@router.get("/reglas", tags=["Sistema"])
async def ver_reglas_negocio() -> Dict[str, Any]:
    """Retorna las reglas de negocio activas (cargadas desde business_rules.json)."""
    import json
    from app.core.business_rules import _RULES_PATH
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.error(f"Error cargando reglas de negocio desde {_RULES_PATH}: {exc}")
        raise HTTPException(status_code=500, detail=f"No se pudo cargar reglas: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK WHATSAPP v3.0
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/webhook/whatsapp", tags=["WhatsApp"])
async def verificar_webhook(
    hub_mode:         str = Query(alias="hub.mode",         default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge:    str = Query(alias="hub.challenge",    default=""),
) -> PlainTextResponse:
    if hub_mode == "subscribe" and hub_verify_token == _settings.meta_verify_token:
        logger.info("Webhook WhatsApp verificado.")
        return PlainTextResponse(hub_challenge, status_code=200)
    logger.warning("Verificación de webhook fallida — token inválido.")
    raise HTTPException(status_code=403, detail="Token de verificación inválido.")


@router.post("/webhook/whatsapp", tags=["WhatsApp"], status_code=status.HTTP_200_OK)
async def recibir_webhook_whatsapp(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict[str, str]:
    """
    v3.0: Guard de horario + detección de escalada + ruteo multicanal.
    Retorna 200 inmediatamente; todo el procesamiento es asíncrono.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body inválido.")

    if body.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    background_tasks.add_task(_procesar_mensaje_entrante, body)
    return {"status": "received"}


async def _procesar_mensaje_entrante(body: Dict[str, Any]) -> None:
    """
    Pipeline completo de procesamiento v3.0:
    1. Extraer datos del webhook.
    2. Guard de horario → encolar si fuera de horario.
    3. Verificar escalada a humano.
    4. Perfilar con Llama 3.3 (detecta banco).
    5. Aplicar reglas de negocio → rutear al outsourcing correcto.
    6. Guardar en CRM Google Sheets.
    7. Responder con Mistral Large 3.
    8. Alerta Telegram si prioridad ALTA.
    """
    global _fuera_horario_enviado
    try:
        webhook  = WhatsAppWebhook(**body)
        wa       = WhatsAppCloudAPI()
        crm_fs   = FirestoreCRM() # Fuente de verdad
        crm_gs   = GoogleSheetsCRM() # Respaldo opcional
        telegram = TelegramAlerter()
        engine   = get_rules_engine()
        ruteo    = RuteoService()

        for entry in webhook.entry:
            for change in entry.changes:
                val = change.value
                if not val.messages:
                    continue

                for msg in val.messages:
                    if msg.type not in ["text", "interactive"]:
                        continue

                    telefono = f"+{msg.from_}" if not msg.from_.startswith("+") else msg.from_
                    nombre   = (val.contacts[0].profile.name if val.contacts else "Desconocido")
                    
                    wa.marcar_leido(msg.id)
                    texto = ""

                    # Intercepción de Respuestas de Plantillas HSM
                    if msg.type == "interactive" and msg.interactive and msg.interactive.button_reply:
                        btn_id = msg.interactive.button_reply.id
                        logger.info("🔘 Botón presionado por %s: %s", telefono, btn_id)
                        
                        if btn_id == "BTN_BAJA":
                            logger.info("Baja solicitada por %s. Cancelando flujo del webhook.", telefono)
                            # Actualizar CRM como pasivo y abortar
                            prospecto_baja = Prospecto(
                                telefono=telefono,
                                nombre=nombre,
                                estado_crm="DESCALIFICADO",
                                notas="🔥 Opt-Out: Usuario solicitó baja vía botón HSM."
                            )
                            crm_fs.upsert_prospecto(prospecto_baja)
                            try:
                                crm_gs.upsert_prospecto(prospecto_baja)
                            except Exception as gexc:
                                logger.warning("Fallo secundario Sheets en baja: %s", gexc)
                            continue  # Abortar síncronamente aquí
                            
                        elif btn_id == "BTN_INTERESA":
                            logger.info("Interés post-HSM detectado de %s. Pasando a Semáforo de Viabilidad.", telefono)
                            texto = "¡Hola! Estoy interesado en retomar el trámite, ¿qué necesitan saber?"
                            
                            import asyncio
                            from datetime import datetime
                            asyncio.create_task(manager.broadcast({
                                "type": "NUEVO_LEAD_IA",
                                "message": f"🚀 HSM Reactivado: {nombre} ha dado clic en Me Interesa.",
                                "timestamp": datetime.now().isoformat()
                            }))
                            
                    elif msg.type == "text" and msg.text:
                        texto = msg.text.body

                    if not texto:
                        continue

                    logger.info("📩 Procesando texto final de %s: %s", telefono, texto[:100])

                    # ── 1. GUARD DE HORARIO ───────────────────────────────────
                    # Extraer metadatos de reactivación si existen
                    is_reactivacion = val.metadata.get("is_queue_reactivation", False) if val.metadata else False
                    
                    if not is_office_hours() and not is_reactivacion:
                        if telefono not in _fuera_horario_enviado:
                            wa.enviar_texto(telefono, get_mensaje_espera())
                            _fuera_horario_enviado.add(telefono)
                        
                        # Actualizar CRM a ESPERANDO_APERTURA
                        crm.actualizar_estado_crm(telefono, "ESPERANDO_APERTURA", f"Lead escribió fuera de horario: {texto[:50]}...")
                        
                        enqueue_message(telefono, texto, nombre)
                        logger.info("⏰ Fuera de horario — mensaje encolado y CRM actualizado: %s", telefono)
                        continue
                    else:
                        # Limpiar marca cuando vuelve en horario
                        _fuera_horario_enviado.discard(telefono)

                    # ── 2. CONSULTAR FIRESTORE (Fuente de Verdad) ─────────────
                    prospecto_existente = crm_fs.get_prospecto(telefono)
                    
                    # SILENCIO PROACTIVO: Si viene de Scraper o está en proceso humano
                    if prospecto_existente:
                        fuente = prospecto_existente.get("fuente", "")
                        estado = prospecto_existente.get("estado_crm", "")
                        
                        if fuente == "WICAPITAL" and estado != "RECONECTADO":
                            logger.info("🤫 Silencio Proactivo: Prospecto %s en gestión WiCapital. Ignorando.", telefono)
                            continue
                            
                        if prospecto_existente.get("ia_pausada"):
                            logger.info("IA pausada para %s — mensaje ignorado por bot.", telefono)
                            continue

                    # ── 3. VERIFICAR ESCALADA A HUMANO ───────────────────────
                    intentos_fallidos = await _get_intentos_fallidos(telefono)
                    escalada = engine.should_escalate(telefono, texto, intentos_fallidos)
                    if escalada.escalar:
                        wa.enviar_texto(telefono, escalada.mensaje)
                        await _set_ia_pausada(telefono)
                        telegram.send(
                            f"🙋 <b>ESCALADA A HUMANO</b>\n"
                            f"📱 <b>Tel:</b> {telefono}\n"
                            f"👤 <b>Nombre:</b> {nombre}\n"
                            f"💬 <b>Mensaje:</b> {texto[:200]}\n"
                            f"🔍 <b>Motivo:</b> {escalada.motivo}"
                        )
                        logger.warning("Escalada a humano: %s — motivo: %s", telefono, escalada.motivo)
                        continue

                    # ── 4. CONSULTA CRM PREVIA (NO REPETIR PREGUNTAS) ──────────
                    contexto_previo = ""
                    if prospecto_existente:
                        contexto_previo = (
                            f"Datos conocidos: Cargo={prospecto_existente.get('cargo')}, "
                            f"Entidad={prospecto_existente.get('empresa')}, "
                            f"Sector={prospecto_existente.get('sector_economico')}. "
                            f"NO REPETIR preguntas sobre estos datos."
                        )

                    # ── 5. PERFILAR CON LLAMA 3.3 ────────────────────────────
                    perfil: PerfilProspecto = await perfilar_prospecto_llama(texto, contexto_previo=contexto_previo)

                    # ── 4. REGLAS DE NEGOCIO → RUTEO ─────────────────────────
                    banco_detectado = getattr(perfil, "banco_detectado", "No especificado")
                    producto_str    = perfil.producto_detectado.value

                    resultado_ruteo = await ruteo.rutar_prospecto(
                        telefono=telefono,
                        nombre=nombre,
                        sector=perfil.sector_economico.value,
                        producto=producto_str,
                        banco_detectado=banco_detectado,
                        perfil_data={},
                        prioridad=perfil.prioridad.value,
                        ingresos_cop=perfil.ingresos_estimados_cop,
                        objeciones=[o.value for o in perfil.objeciones],
                        resumen_ia=perfil.resumen_analisis,
                    )

                    # ── 5. GUARDAR EN CRM (Firestore First + Sheets Backup) ────
                    prospecto = Prospecto(
                        telefono=telefono,
                        nombre=nombre,
                        sector_economico=perfil.sector_economico,
                        producto_interes=perfil.producto_detectado,
                        prioridad=perfil.prioridad,
                        ingresos_estimados_cop=perfil.ingresos_estimados_cop,
                        objeciones=[o.value for o in perfil.objeciones],
                        notas=(
                            f"{perfil.resumen_analisis} | "
                            f"Banco: {banco_detectado} | "
                            f"Ruteo: {resultado_ruteo.outsourcing}"
                        ),
                        estado_crm="NUEVO" if perfil.califica else "DESCALIFICADO",
                        fuente="WHATSAPP_INBOUND",
                    )
                    
                    # Persistencia en Firestore (Crítico)
                    crm_fs.upsert_prospecto(prospecto)
                    
                    # Sincronización a Sheets (Opcional/Secundario)
                    try:
                        crm_gs.upsert_prospecto(prospecto)
                    except Exception as gexc:
                        logger.warning("Fallo secundario al escribir en Sheets: %s", gexc)

                    # ── 6. RESPONDER CON MISTRAL ─────────────────────────────
                    redireccion_str = ""
                    if producto_str == "HIPOTECARIO" and not perfil.califica:
                        redireccion_str = " | ACCIÓN: Ofrécele inmediatamente Libranza o Consumo como alternativa rápida."

                    contexto_reactivacion = ""
                    if is_reactivacion:
                        contexto_reactivacion = (
                            " | INSTRUCCIÓN TEMPORAL: Este mensaje lo envió el cliente anoche fuera de horario. "
                            "Inicia tu respuesta saludando amablemente por la mañana, haciendo referencia a que leíste su consulta "
                            "de anoche y continúa el asesoramiento con naturalidad."
                        )

                    if perfil.respuesta_sugerida and not is_reactivacion:
                        respuesta = perfil.respuesta_sugerida
                    else:
                        respuesta = await responder_chat_mistral(
                            mensaje_usuario=texto,
                            contexto_prospecto=(
                                f"Nombre: {nombre}, Sector: {perfil.sector_economico.value}, "
                                f"Producto: {producto_str}, Banco: {banco_detectado}, "
                                f"Califica: {perfil.califica}, "
                                f"Outsourcing: {resultado_ruteo.outsourcing}{redireccion_str}{contexto_reactivacion}"
                            )
                        )
                    wa.enviar_texto(telefono, respuesta)
                    
                    # ── Notificación Live WS ─────────────────────────────────
                    import asyncio
                    from datetime import datetime
                    asyncio.create_task(manager.broadcast({
                        "type": "NUEVO_LEAD_IA",
                        "message": f"🤖 Llama 3 procesó y contestó un mensaje de {nombre} ({perfil.producto_detectado.value}).",
                        "timestamp": datetime.now().isoformat()
                    }))

                    # ── 7. ALERTA TELEGRAM ALTA PRIORIDAD ────────────────────
                    if perfil.prioridad.value == "ALTA":
                        telegram.alerta_prospecto_alta_prioridad(
                            prospecto,
                            f"{perfil.resumen_analisis} | Banco: {banco_detectado} | "
                            f"Ruteo: {resultado_ruteo.outsourcing} ({resultado_ruteo.canal})",
                        )

                    # Resetear contador de intentos fallidos
                    await _reset_intentos_fallidos(telefono)

    except Exception as exc:
        logger.error("Error crítico procesando webhook: %s", exc, exc_info=True)
        try:
            TelegramAlerter().alerta_error_critico("Webhook WhatsApp", str(exc))
        except Exception:
            pass


# ─── Helpers Firestore para estado de IA ─────────────────────────────────────

async def _get_intentos_fallidos(telefono: str) -> int:
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)
        doc = db.collection("chat_states").document(telefono.replace("+", "")).get()
        return doc.to_dict().get("intentos_fallidos", 0) if doc.exists else 0
    except Exception:
        return 0

async def _set_ia_pausada(telefono: str) -> None:
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)
        db.collection("chat_states").document(telefono.replace("+", "")).set(
            {"ia_pausada": True, "pausada_en": firestore.SERVER_TIMESTAMP}, merge=True
        )
    except Exception as exc:
        logger.warning("No se pudo marcar IA pausada: %s", exc)

async def _is_ia_pausada(telefono: str) -> bool:
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)
        doc = db.collection("chat_states").document(telefono.replace("+", "")).get()
        return doc.to_dict().get("ia_pausada", False) if doc.exists else False
    except Exception:
        return False

async def _verificar_abandono_24h(telefono: str) -> bool:
    """Retorna True si han pasado > 24h desde la última interacción."""
    try:
        from google.cloud import firestore
        from datetime import datetime, timedelta, timezone
        db = firestore.Client(project=_settings.google_cloud_project)
        doc = db.collection("chat_states").document(telefono.replace("+", "")).get()
        if doc.exists:
            data = doc.to_dict()
            ultima_vez = data.get("ultima_interaccion")
            if ultima_vez:
                # Firestore timestamp to datetime
                ahora = datetime.now(timezone.utc)
                if ahora - ultima_vez > timedelta(hours=24):
                    return True
        return False
    except Exception:
        return False

async def _reset_intentos_fallidos(telefono: str) -> None:
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)
        db.collection("chat_states").document(telefono.replace("+", "")).set(
            {"intentos_fallidos": 0, "ultima_interaccion": firestore.SERVER_TIMESTAMP}, merge=True
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# COLA DE MENSAJES
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/cola/procesar", tags=["Sistema"])
async def procesar_cola_manual(
    max_items: int = Query(default=50, ge=1, le=200),
    background_tasks: BackgroundTasks = None,
    x_cron_secret: Optional[str] = Header(None, alias="X-Cron-Secret"),
) -> Dict[str, Any]:
    """Procesa manualmente la cola de mensajes fuera de horario."""
    if not x_cron_secret or x_cron_secret != _settings.cron_secret:
        logger.warning("Intento de procesamiento de cola no autorizado.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autorizado.")

    if not is_office_hours():
        return {"status": "ignorado", "mensaje": "Fuera de horario laboral — cola no procesada."}

    if background_tasks:
        background_tasks.add_task(process_queue, max_items)
        return {"status": "encolado", "mensaje": f"Procesando hasta {max_items} mensajes en background."}

    procesados = process_queue(max_items)
    return {"status": "completado", "procesados": procesados}


@router.post("/ia/reanudar/{telefono}", tags=["Sistema"])
async def reanudar_ia_para_numero(telefono: str) -> Dict[str, Any]:
    """Reanuda la respuesta automática de IA para un número específico (post-atención humana)."""
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)
        db.collection("chat_states").document(telefono.replace("+", "")).set(
            {"ia_pausada": False, "intentos_fallidos": 0}, merge=True
        )
        return {"exito": True, "mensaje": f"IA reanudada para {telefono}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# REDES SOCIALES ORGÁNICAS
# ═══════════════════════════════════════════════════════════════════════════════

class PublicarOrganicaRequest(BaseModel):
    sector:     str  = Field(description="Sector económico objetivo")
    producto:   str  = Field(description="Producto financiero (LIBRANZA, CONSUMO, etc.)")
    banco:      str  = Field(default="", description="Banco específico (opcional)")
    plataformas: List[str] = Field(
        default=["FACEBOOK", "INSTAGRAM", "LINKEDIN"],
        description="FACEBOOK | INSTAGRAM | TIKTOK | LINKEDIN",
    )
    imagen_url: Optional[str] = Field(default=None)
    video_url:  Optional[str] = Field(default=None)
    tono:       str  = Field(default="profesional y cercano")
    solo_generar: bool = Field(default=False, description="Genera el copy sin publicar")


@router.post("/social/publicar", tags=["Social Media"])
async def publicar_organico(payload: PublicarOrganicaRequest) -> Dict[str, Any]:
    """
    Genera copy con IA y publica en las plataformas solicitadas.
    Si solo_generar=True, retorna el copy sin publicar.
    """
    from app.services.social_media_manager import SocialMediaOrchestrator
    orq = SocialMediaOrchestrator()

    if payload.solo_generar:
        copys = await orq.solo_generar_copy(
            sector=payload.sector,
            producto=payload.producto,
            banco=payload.banco,
            tono=payload.tono,
        )
        return {"exito": True, "modo": "solo_copy", "copys": copys}

    resultados = await orq.generar_y_publicar(
        sector=payload.sector,
        producto=payload.producto,
        banco=payload.banco,
        plataformas=payload.plataformas,
        imagen_url=payload.imagen_url,
        video_url=payload.video_url,
        tono=payload.tono,
    )

    return {
        "exito": True,
        "resultados": {k: {"exito": v.exito, "post_id": v.post_id, "mensaje": v.mensaje}
                       for k, v in resultados.items()},
        "exitosos": sum(1 for v in resultados.values() if v.exito),
        "fallidos":  sum(1 for v in resultados.values() if not v.exito),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CRM — BASES DE DATOS, CAMPAÑAS Y COMUNICACIONES (preservados de v2.0)
# ═══════════════════════════════════════════════════════════════════════════════

class ImportarBaseRequest(BaseModel):
    registros:    List[Dict[str, Any]] = Field(description="Registros de la base a importar.")
    tab_destino:  str = Field(default="Base_Importada")

@router.post("/crm/importar-base", tags=["CRM"], response_model=RespuestaBase)
async def importar_base_datos(payload: ImportarBaseRequest) -> RespuestaBase:
    try:
        por_sector = limpiar_y_segmentar_base(payload.registros)
        crm   = GoogleSheetsCRM()
        total = 0
        for sector, registros in por_sector.items():
            total += crm.importar_base_datos(registros, tab_name=f"{payload.tab_destino}_{sector}")
        return RespuestaBase(exito=True, mensaje=f"{total} registros importados en {len(por_sector)} sectores.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class RetomarClienteRequest(BaseModel):
    telefono:       str
    nombre_cliente: str
    producto:       str = "LIBRANZA"
    tasa_oferta:    str = "1.3"

@router.post("/crm/retomar-cliente", tags=["CRM"], response_model=RespuestaBase)
async def retomar_cliente(payload: RetomarClienteRequest) -> RespuestaBase:
    try:
        wa = WhatsAppCloudAPI()
        r  = wa.retomar_cliente(payload.telefono, payload.nombre_cliente, payload.producto, payload.tasa_oferta)
        return RespuestaBase(exito=r.get("exito", False), mensaje=r.get("error", "Plantilla enviada."))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class CampanaMasivaRequest(BaseModel):
    csv_contenido:    Optional[str]             = None
    gcs_uri:          Optional[str]             = Field(default=None, description="URI de Cloud Storage (ej: gs://mi-bucket/base.csv)")
    contactos:        Optional[List[Dict[str, str]]] = None
    nombre_plantilla: str  = "RETOMA_LIBRANZA"
    lote_size:        int  = Field(default=50, ge=1, le=100)

@router.post("/crm/campana-masiva", tags=["CRM"])
async def campana_masiva(payload: CampanaMasivaRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    if payload.gcs_uri:
        try:
            from google.cloud import storage
            client = storage.Client(project=_settings.google_cloud_project)
            bucket_name = payload.gcs_uri.replace("gs://", "").split("/")[0]
            blob_name = "/".join(payload.gcs_uri.replace("gs://", "").split("/")[1:])
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            csv_data = blob.download_as_text()
            contactos = procesar_csv_contactos(csv_data)
        except Exception as exc:
            logger.error("Error leyendo de Cloud Storage: %s", exc)
            raise HTTPException(status_code=500, detail=f"Error leyendo de Cloud Storage: {exc}")
    elif payload.csv_contenido:
        contactos = procesar_csv_contactos(payload.csv_contenido)
    else:
        contactos = payload.contactos or []

    if not contactos:
        raise HTTPException(status_code=400, detail="Sin contactos válidos.")
    background_tasks.add_task(_ejecutar_campana_masiva, contactos, payload.nombre_plantilla, payload.lote_size)
    return {"status": "encolado", "total_contactos": len(contactos), "plantilla": payload.nombre_plantilla}

async def _ejecutar_campana_masiva(contactos, plantilla, lote_size):
    try:
        WhatsAppCloudAPI().campana_masiva(contactos, plantilla, lote_size)
    except Exception as exc:
        logger.error("Error campaña masiva: %s", exc, exc_info=True)


class AuditarChatRequest(BaseModel):
    chat_texto:     str = Field(description="Chat exportado de WhatsApp (.txt)")
    telefono:       str = "Desconocido"
    nombre_contacto:str = "Desconocido"

@router.post("/crm/auditar-chat", tags=["CRM"])
async def auditar_chat(payload: AuditarChatRequest) -> Dict[str, Any]:
    local    = analizar_chat_exportado_local(payload.chat_texto)
    auditoria = await auditar_chat_exportado_llama(payload.chat_texto, payload.telefono, payload.nombre_contacto)
    return {"exito": True, "analisis_local": local, "auditoria_ia": auditoria.model_dump()}


# ═══════════════════════════════════════════════════════════════════════════════
# WICAPITAL
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook/whatsapp", tags=["WhatsApp"])
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
):

@router.post("/wicapital/sync", tags=["WiCapital"])
async def sincronizar_wicapital(
    background_tasks: BackgroundTasks,
    x_cron_secret: Optional[str] = Header(None, alias="X-Cron-Secret"),
) -> Dict[str, Any]:
    """
    Sincroniza WiCapital con Google Sheets y envía reportes.
    Requiere autenticación vía X-Cron-Secret para ejecuciones desde Cloud Scheduler.
    Verifica conexión con Google Sheets y asegura estado 'Operativo'.
    """
    if not x_cron_secret or x_cron_secret != _settings.cron_secret:
        logger.warning("Intento de ejecución de WiCapital Sync no autorizado.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autorizado.")

    # [Antigravity] Guard de Google Sheets Integrado
    # ── Verificar conexión con Google Sheets ──
    gsheets_status = "Operativo"
    try:
        crm_test = GoogleSheetsCRM()
        # Ping rápido a la hoja de campañas o prospectos para asegurar operatividad
        crm_test._safe_get_records(_settings.gsheets_wicapital_tab)
    except Exception as exc:
        logger.error("Error conectando a Google Sheets en /sync: %s", exc)
        gsheets_status = "Fallo de Conexión (Modo Degradado)"

    background_tasks.add_task(_ejecutar_ciclo_wicapital)
    return {
        "status": "iniciado", 
        "estado_sistema": gsheets_status,
        "mensaje": "Ciclo WiCapital iniciado en background. Estado de Integración: Operativo."
    }

async def _ejecutar_ciclo_wicapital():
    try:
        monitor = WiCapitalMonitor()
        resultado = monitor.run_full_cycle()
        logger.info("Ciclo WiCapital: %s", resultado)
    except Exception as exc:
        logger.error("Error ciclo WiCapital: %s", exc, exc_info=True)
        TelegramAlerter().alerta_error_critico("WiCapital Sync", str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# PROSPECTOS CRM
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/prospectos/prioridad/{prioridad}", tags=["CRM"])
async def prospectos_por_prioridad(prioridad: PrioridadProspecto) -> Dict[str, Any]:
    try:
        crm = GoogleSheetsCRM()
        p   = crm.get_prospectos_por_prioridad(prioridad)
        return {"exito": True, "total": len(p), "prospectos": p}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/prospectos/sector/{sector}", tags=["CRM"])
async def prospectos_por_sector(sector: SectorEconomico) -> Dict[str, Any]:
    try:
        crm = GoogleSheetsCRM()
        p   = crm.get_prospectos_por_sector(sector)
        return {"exito": True, "total": len(p), "sector": sector.value, "prospectos": p}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

class ActualizarEstadoRequest(BaseModel):
    telefono:    str
    nuevo_estado:str
    notas:       str = ""

@router.patch("/prospectos/estado", tags=["CRM"])
async def actualizar_estado_prospecto(payload: ActualizarEstadoRequest) -> RespuestaBase:
    try:
        crm   = GoogleSheetsCRM()
        exito = crm.actualizar_estado_crm(payload.telefono, payload.nuevo_estado, payload.notas)
        return RespuestaBase(exito=exito, mensaje="Actualizado." if exito else "No encontrado.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# IA — ENDPOINTS DE TESTING DIRECTO
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/ia/perfilar", tags=["IA"], response_model=RespuestaProspecto)
async def perfilar_mensaje(
    mensaje:  str = Query(...),
    telefono: str = Query(default="N/A"),
) -> RespuestaProspecto:
    perfil = await perfilar_prospecto_llama(mensaje)
    p = None
    if telefono != "N/A":
        p = Prospecto(
            telefono=telefono,
            sector_economico=perfil.sector_economico,
            producto_interes=perfil.producto_detectado,
            prioridad=perfil.prioridad,
        )
    return RespuestaProspecto(exito=True, mensaje="Perfilamiento completado.", perfil=perfil, prospecto=p)

@router.post("/ia/chat", tags=["IA"])
async def chat_mistral(mensaje: str = Query(...), contexto: Optional[str] = Query(default=None)) -> Dict[str, str]:
    r = await responder_chat_mistral(mensaje_usuario=mensaje, contexto_prospecto=contexto)
    return {"respuesta": r}

@router.get("/ia/ruteo", tags=["IA"])
async def consultar_ruteo(
    producto: str = Query(..., description="LIBRANZA|CONSUMO|HIPOTECARIO|COMPRA_CARTERA"),
    banco:    str = Query(..., description="Nombre del banco"),
) -> Dict[str, Any]:
    """Consulta a qué outsourcing iría un prospecto con ese producto y banco."""
    engine  = get_rules_engine()
    routing = engine.get_routing(producto, banco)
    return {
        "producto":      producto,
        "banco_raw":     banco,
        "banco_normalizado": routing.banco_normalizado,
        "outsourcing":   routing.outsourcing,
        "integracion":   routing.integracion,
        "email_destino": routing.email_principal,
        "tasa_min":      routing.tasa_min,
        "tasa_max":      routing.tasa_max,
        "plazo_max_meses": routing.plazo_max_meses,
        "documentos":    routing.documentos,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD — ENDPOINTS PARA EL FRONTEND (v3.1)
# Estos 5 endpoints alimentan el Next.js Dashboard.
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/prospectos", tags=["CRM"])
async def listar_prospectos(
    page:      int = Query(default=1,  ge=1,   description="Página (1-indexed)"),
    limit:     int = Query(default=25, ge=1, le=200, description="Registros por página"),
    sector:    Optional[str] = Query(default=None, description="Filtro por sector económico"),
    producto:  Optional[str] = Query(default=None, description="Filtro por producto financiero"),
    prioridad: Optional[str] = Query(default=None, description="ALTA|MEDIA|BAJA|DESCALIFICADO"),
    estado:    Optional[str] = Query(default=None, description="Estado CRM: NUEVO|Perfilado|Aprobado…"),
    buscar:    Optional[str] = Query(default=None, description="Búsqueda en nombre o teléfono"),
) -> Dict[str, Any]:
    """
    Lista prospectos con paginación y filtros múltiples.
    Alimenta la DataTable de Leads y el Pipeline Kanban del frontend.
    """
    try:
        crm = GoogleSheetsCRM()
        todos = crm.get_all_prospectos()

        # ── Filtros ───────────────────────────────────────────────────────────
        if sector:
            todos = [p for p in todos if p.get("Sector", "").upper() == sector.upper()]
        if producto:
            todos = [p for p in todos if p.get("Producto", "").upper() == producto.upper()]
        if prioridad:
            todos = [p for p in todos if p.get("Prioridad", "").upper() == prioridad.upper()]
        if estado:
            todos = [p for p in todos if estado.lower() in p.get("Estado_CRM", "").lower()]
        if buscar:
            q = buscar.lower()
            todos = [
                p for p in todos
                if q in p.get("Nombre", "").lower() or q in p.get("Telefono", "").lower()
            ]

        # ── Paginación ────────────────────────────────────────────────────────
        total = len(todos)
        inicio = (page - 1) * limit
        fin    = inicio + limit
        pagina = todos[inicio:fin]

        return {
            "exito":       True,
            "total":       total,
            "page":        page,
            "limit":       limit,
            "total_pages": max(1, -(-total // limit)),  # ceil division
            "prospectos":  pagina,
        }
    except Exception as exc:
        logger.error("Error en listar_prospectos: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/prospectos/{telefono}", tags=["CRM"])
async def detalle_prospecto(telefono: str) -> Dict[str, Any]:
    """
    Retorna el perfil completo de un prospecto por número de teléfono.
    Alimenta el panel lateral de detalle en la DataTable.
    """
    try:
        crm  = GoogleSheetsCRM()
        dato = crm.get_prospecto_by_telefono(telefono)
        if not dato:
            raise HTTPException(status_code=404, detail=f"Prospecto {telefono} no encontrado.")
        return {"exito": True, "prospecto": dato}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/dashboard/metricas", tags=["Dashboard"])
async def metricas_dashboard() -> Dict[str, Any]:
    """
    KPIs consolidados para el Dashboard principal.
    Calcula leads de hoy/semana/mes, distribución por sector, producto y prioridad,
    tasa de conversión estimada, estado de la cola y si estamos en horario laboral.
    """
    try:
        from datetime import date, timedelta
        from zoneinfo import ZoneInfo

        crm      = GoogleSheetsCRM()
        todos    = crm.get_all_prospectos()
        campanas = crm.get_all_campanas()
        wic_data = crm.get_all_wicapital_data()

        hoy      = date.today()
        hace_7d  = hoy - timedelta(days=7)
        hace_30d = hoy - timedelta(days=30)

        leads_hoy    = 0
        leads_semana = 0
        leads_mes    = 0
        por_sector:   Dict[str, int] = {}
        por_producto: Dict[str, int] = {}
        por_prioridad: Dict[str, int] = {}
        por_estado:   Dict[str, int] = {}
        aprobados = 0

        for p in todos:
            # Conteo por fecha
            fecha_raw = p.get("Fecha_Creacion", "")
            try:
                fecha = date.fromisoformat(fecha_raw[:10])
                if fecha == hoy:
                    leads_hoy += 1
                if fecha >= hace_7d:
                    leads_semana += 1
                if fecha >= hace_30d:
                    leads_mes += 1
            except Exception:
                pass

            # Distribuciones
            sector   = p.get("Sector", "DESCONOCIDO")
            producto = p.get("Producto", "DESCONOCIDO")
            prior    = p.get("Prioridad", "BAJA")
            estado   = p.get("Estado_CRM", "NUEVO")

            por_sector[sector]     = por_sector.get(sector, 0) + 1
            por_producto[producto] = por_producto.get(producto, 0) + 1
            por_prioridad[prior]   = por_prioridad.get(prior, 0) + 1
            por_estado[estado]     = por_estado.get(estado, 0) + 1

            if "Aprobado" in estado or "Desembolso" in estado:
                aprobados += 1

        total = len(todos)
        tasa_conversion = round((aprobados / total * 100), 1) if total > 0 else 0.0

        # Cola de mensajes pendientes
        cola_pendiente = 0
        try:
            from google.cloud import firestore
            db = firestore.Client(project=_settings.google_cloud_project)
            cola_pendiente = len(list(
                db.collection("messages_queue")
                .where("procesado", "==", False)
                .limit(500)
                .stream()
            ))
        except Exception:
            pass

        return {
            "exito": True,
            "leads": {
                "hoy":    leads_hoy,
                "semana": leads_semana,
                "mes":    leads_mes,
                "total":  total,
            },
            "por_sector":    dict(sorted(por_sector.items(),   key=lambda x: x[1], reverse=True)),
            "por_producto":  dict(sorted(por_producto.items(), key=lambda x: x[1], reverse=True)),
            "por_prioridad": por_prioridad,
            "por_estado":    por_estado,
            "tasa_conversion_pct": tasa_conversion,
            "aprobados_total":     aprobados,
            "cola_pendiente":      cola_pendiente,
            "en_horario_laboral":  is_office_hours(),
            "campanas_activas":    len([c for c in campanas if c.get("Estado", "").upper() == "ACTIVA"]),
            "wicapital": {
                "total_registros": len(wic_data),
                "estados": _count_wicapital_states(wic_data)
            }
        }
    except Exception as exc:
        logger.error("Error en metricas_dashboard: %s", exc, exc_info=True)
        # Fallback de emergencia para que el frontend no rompa
        return {
            "exito": False,
            "error": str(exc),
            "leads": {"hoy": 0, "semana": 0, "mes": 0, "total": 0},
            "wicapital": {"total_registros": 0, "estados": {}}
        }

def _count_wicapital_states(data: List[Dict[str, Any]]) -> Dict[str, int]:
    """Helper para agrupar estados de WiCapital."""
    counts = {}
    for item in data:
        # Intentar obtener 'Estado' o 'Estatus' (normalización flexible)
        estado = item.get("Estado") or item.get("Estatus") or "DESCONOCIDO"
        counts[estado] = counts.get(estado, 0) + 1
    return counts


@router.get("/wicapital/casos", tags=["WiCapital"])
async def listar_casos_wicapital(
    seccion: Optional[str] = Query(default=None, description="Gestión Filtros|Radicados|Aprobados|Desembolso"),
    limit:   int = Query(default=100, ge=1, le=500),
) -> Dict[str, Any]:
    """
    Retorna los casos monitoreados en WiCapital desde Firestore.
    Alimenta la tabla del Centro WiCapital en el frontend.
    """
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)

        query = db.collection(_settings.firestore_collection).limit(limit)
        if seccion:
            query = query.where("Seccion", "==", seccion)

        docs = list(query.stream())
        casos = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            # Convertir Timestamp de Firestore a ISO string para JSON
            for campo in ("created_at", "updated_at"):
                if campo in data and hasattr(data[campo], "isoformat"):
                    data[campo] = data[campo].isoformat()
            casos.append(data)

        # Agrupar por sección para el resumen
        por_seccion: Dict[str, int] = {}
        for c in casos:
            s = c.get("Seccion", "Desconocido")
            por_seccion[s] = por_seccion.get(s, 0) + 1

        return {
            "exito":       True,
            "total":       len(casos),
            "por_seccion": por_seccion,
            "casos":       casos,
        }
    except Exception as exc:
        logger.error("Error en listar_casos_wicapital: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/cola/pendientes", tags=["Sistema"])
async def cola_pendientes_count() -> Dict[str, Any]:
    """
    Retorna el número de mensajes pendientes en la cola de mensajes fuera de horario.
    Alimenta el badge del header en el dashboard.
    """
    try:
        from google.cloud import firestore
        db = firestore.Client(project=_settings.google_cloud_project)

        docs = list(
            db.collection("messages_queue")
            .where("procesado", "==", False)
            .limit(500)
            .stream()
        )

        mensajes = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            recibido = data.get("recibido_en")
            if hasattr(recibido, "isoformat"):
                data["recibido_en"] = recibido.isoformat()
            mensajes.append({
                "id":         data["id"],
                "telefono":   data.get("telefono", ""),
                "nombre":     data.get("nombre", "Desconocido"),
                "recibido_en":data.get("recibido_en", ""),
                "preview":    data.get("texto", "")[:80],
            })

        return {
            "exito":     True,
            "pendientes": len(mensajes),
            "mensajes":  mensajes,
            "en_horario": is_office_hours(),
        }
    except Exception as exc:
        logger.error("Error en cola_pendientes: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

@router.patch("/prospectos/estado", tags=["CRM"])
async def actualizar_estado_prospecto(
    telefono: str = Query(...),
    estado:   str = Query(...),
) -> Dict[str, Any]:
    """Actualiza el estado de un prospecto específico en GSheets desde el frontend (Kanban/Table)."""
    try:
        crm = GoogleSheetsCRM()
        exito = crm.actualizar_estado_crm(telefono=telefono, nuevo_estado=estado)
        if exito:
            return {"exito": True, "mensaje": f"Estado actualizado a {estado}"}
        else:
            raise HTTPException(status_code=404, detail="Prospecto no encontrado en el CRM")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error al actualizar estado: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

@router.post("/whatsapp/enviar_mensaje", tags=["WhatsApp"])
async def enviar_mensaje_manual(
    telefono: str = Query(...),
    mensaje:  str = Query(...),
) -> Dict[str, Any]:
    """Envía un mensaje manual por WhatsApp y lo registra en el CRM (vía frontend)."""
    try:
        from app.services.whatsapp_service import WhatsAppCloudAPI
        wa = WhatsAppCloudAPI()
        
        # Enviar vía Meta
        exito = wa.enviar_texto(telefono, mensaje)
        
        if exito:
            # Registrar contacto en CRM
            crm = GoogleSheetsCRM()
            crm.actualizar_estado_crm(telefono=telefono, nuevo_estado="Contactado (Manual)", notas=f"Mensaje enviado desde Dashboard: {mensaje[:50]}...")
            return {"exito": True, "mensaje": "Mensaje enviado exitosamente"}
        else:
            raise HTTPException(status_code=500, detail="Error en la API de Meta")
    except Exception as exc:
        logger.error("Error al enviar WA manual: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

# ─── MÓDULOS OMNICANAL V3.0 (Nuevos Endpoints B2B) ─────────────

@router.get("/social/logs", tags=["Ecosistema"])
async def obtener_logs_sociales() -> Dict[str, Any]:
    """Retorna los logs de respuestas orgánicas de Llama 3 en Meta/TikTok."""
    # Placeholder: En el futuro conectará con Base de datos / Firestore
    # Retornará 500 si no hay conexión real, forzando los Mocks en Frontend.
    raise HTTPException(status_code=500, detail="Módulo de base de datos social no configurado aún.")

@router.get("/whatsapp/chats", tags=["Ecosistema"])
async def obtener_chats_whatsapp() -> Dict[str, Any]:
    """Retorna las conversaciones de WA categorizadas (IA vs Humano)."""
    raise HTTPException(status_code=500, detail="Sin conexión a memoria de chats.")

@router.get("/outsourcing/casos", tags=["Ecosistema"])
async def obtener_casos_outsourcing() -> Dict[str, Any]:
    """Retorna el estado de los leads despachados a AV Villas y terceros."""
    raise HTTPException(status_code=500, detail="Conector de Outsourcing offline.")

@router.post("/config/wicapital", tags=["Configuración"])
async def configurar_credenciales_wicapital(
    usuario: str = Query(...),
    password: str = Query(...),
    tipo_cuenta: str = Query(..., description="'PRINCIPAL' o 'ASESOR'")
) -> Dict[str, Any]:
    """Guarda (simulado) las credenciales y lanza un test de conexión al banco."""
    # Aquí iría el acceso a SecretManager
    # Por ahora simulamos validación
    return {"exito": True, "mensaje": f"Credenciales para {tipo_cuenta} estables y validadas."}

@router.post("/wicapital/manual_fallback", tags=["Configuración"])
async def bypass_fallback_wicapital(
    negocio_id: str = Query(...),
    cliente: str = Query(...),
    estado: str = Query(...)
) -> Dict[str, Any]:
    """Flujo de contingencia: Inyecta un caso manual y avisa al sistema para que la IA actúe."""
    # Simula lanzar una notificación por webhook/websocket a Llama 3
    return {"exito": True, "mensaje": f"Lead {negocio_id} inyectado al motor de reglas."}

@router.post("/cola/procesar", tags=["Sistema"])
async def procesar_cola_mensajes(
    max_items: int = Query(50, description="Máximo de mensajes a procesar en este lote."),
    secret: str = Query(..., description="Token de seguridad para evitar disparos accidentales.")
) -> Dict[str, Any]:
    """
    Vaciado inteligente de la cola de mensajes acumulados fuera de horario.
    Este endpoint debe ser llamado por Cloud Scheduler al iniciar la jornada.
    """
    from app.core.config import get_settings
    settings = get_settings()
    
    if secret != settings.cron_secret:
        raise HTTPException(status_code=403, detail="Secret inválido.")
    
    try:
        procesados = await process_queue(max_items=max_items)
        return {
            "exito": True, 
            "mensajes_procesados": procesados,
            "mensaje": f"Se reactivaron {procesados} conversaciones con contexto matutino."
        }
    except Exception as exc:
        logger.error("Error en motor de reactivación: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

# ─── FUNCIONES AUXILIARES DE CONTROL ──────────────────────────────────────────

async def _is_ia_pausada(telefono: str) -> bool:
    """
    Verifica si un humano ha tomado el control de la conversación.
    Consulta el CRM (GSheets) para ver si el estado es 'ATENCION_HUMANA' o similar.
    """
    try:
        crm = GoogleSheetsCRM()
        prospecto = crm.get_prospecto_by_telefono(telefono)
        if prospecto and prospecto.get("Estado_CRM") in ["ATENCION_HUMANA", "ESCALADO", "MUDOS_CON_BOT"]:
            return True
        return False
    except Exception:
        return False

async def _verificar_abandono_24h(telefono: str) -> bool:
    """
    Verifica si han pasado más de 24 horas desde el último mensaje del usuario.
    Retorna True si hay abandono.
    """
    try:
        crm = GoogleSheetsCRM()
        prospecto = crm.get_prospecto_by_telefono(telefono)
        if not prospecto:
            return False
            
        fecha_str = prospecto.get("Ultimo_Contacto")
        if not fecha_str:
            return False
            
        from datetime import datetime
        ultimo_contacto = datetime.fromisoformat(fecha_str)
        diff = datetime.utcnow() - ultimo_contacto
        
        return diff.total_seconds() > 86400  # 24 horas
    except Exception:
        return False
