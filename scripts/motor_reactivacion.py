"""
Motor de Reactivación Outbound v3.1 - Ecosistema Omnicanal Financiero
Worker asíncrono para despachar Plantillas HSM interactivos pre-aprobados por Meta.
Consume la función enviar_plantilla_hsm del WhatsApp Service.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from app.core.config import get_settings
from app.core.logging_config import configure_logging
from app.services.whatsapp_service import enviar_plantilla_hsm

# ─── Configuración centralizadada ─────────────────────────────────────────────
_settings = get_settings()
configure_logging(_settings.app_env)
logger = logging.getLogger("OutboundEngine")

# Controles de Rate Limit (Meta: ~80msg/s en Tier 1)
MAX_CONCURRENT_WORKERS = 5    # Semáforo: máx 5 goroutines paralelas
BATCH_DELAY_SECONDS   = 1.2  # Pausa entre lotes → ~50 msg/min seguros
BATCH_SIZE            = 10   # Leads por lote antes de pausar

# ─── Modelo de Lead (en producción viremos a un Firestore/BigQuery dataclass) ──
@dataclass
class Lead:
    id_cliente: str
    telefono: str
    nombre: str
    producto: str = "LIBRANZA"
    estado: str = "DORMANT"


# ─── Conector de datos (reemplazar con DB real) ───────────────────────────────
async def _extraer_leads_dormantes() -> List[Lead]:
    """
    En producción: conectar a Firestore, BigQuery o Google Sheets.
    Retorna leads con estado DORMANT o SIN_RESPUESTA.
    """
    return [
        Lead("C-001", "+57300000001", "Carlos Vega",    "LIBRANZA"),
        Lead("C-002", "+57300000002", "Ana Ríos",       "CONSUMO"),
        Lead("C-003", "+57300000003", "Jorge Luna",     "COMPRA_CARTERA"),
        Lead("C-004", "+57310000004", "Sofía Torres",   "LIBRANZA"),
        Lead("C-005", "+57310000005", "Luis Herrera",   "LIBRANZA"),
    ]


# ─── Mapeo de producto → plantilla HSM ───────────────────────────────────────
PLANTILLAS_POR_PRODUCTO = {
    "LIBRANZA":       "bienvenida_multiproducto_v1",
    "CONSUMO":        "retoma_consumo_v2",
    "COMPRA_CARTERA": "retoma_compra_cartera_v1",
    "HIPOTECARIO":    "retoma_hipotecario_v1",
}

def _resolver_plantilla(producto: str) -> str:
    return PLANTILLAS_POR_PRODUCTO.get(
        producto.upper(),
        "bienvenida_multiproducto_v1"  # Fallback a plantilla genérica
    )


# ─── Worker por lead (protegido por semáforo) ─────────────────────────────────
async def _despachar_lead(
    lead: Lead,
    semaphore: asyncio.Semaphore,
) -> None:
    """
    Despacha la plantilla HSM para un lead.
    - Acuadrado por semáforo → máx MAX_CONCURRENT_WORKERS simultáneos.
    - Excepción capturada → no interrumpe el worker global.
    """
    async with semaphore:
        nombre_plantilla = _resolver_plantilla(lead.producto)
        try:
            resultado = await enviar_plantilla_hsm(
                numero_destino=lead.telefono,
                nombre_plantilla=nombre_plantilla,
                # Variables: {{1}} = nombre del lead (como aprobado en Meta BM)
                variables=[lead.nombre],
                idioma="es_CO",
            )

            if resultado.get("exito"):
                logger.info(
                    "[OUTBOUND | OK  ] id=%s | tel=%s | plantilla='%s' | msg_id=%s",
                    lead.id_cliente, lead.telefono,
                    nombre_plantilla, resultado.get("msg_id"),
                )
            else:
                logger.error(
                    "[OUTBOUND | FAIL] id=%s | tel=%s | plantilla='%s' | error=%s",
                    lead.id_cliente, lead.telefono,
                    nombre_plantilla, resultado.get("error"),
                )

        except Exception as exc:
            # Error no previsto → log crítico pero NO detiene el motor
            logger.critical(
                "[OUTBOUND | CRIT] id=%s | tel=%s | exc=%s",
                lead.id_cliente, lead.telefono, str(exc),
            )

        # Micro-throttle dentro del semáforo para distribución uniforme
        await asyncio.sleep(0.15)


# ─── Orquestador principal ────────────────────────────────────────────────────
async def arrancar_motor_reactivacion() -> None:
    """
    Entry point del worker de reactivación.
    Flujo:
      1. Extrae leads dormantes.
      2. Crea semáforo de concurrencia.
      3. Itera en lotes (BATCH_SIZE) con pausa entre ellos.
      4. gather() ejecuta cada lote en paralelo controlado.
    """
    logger.info("═══ Motor Reactivación Outbound v3.1 arrancando... ═══")

    leads = await _extraer_leads_dormantes()
    total = len(leads)

    if total == 0:
        logger.info("No hay leads dormantes. Motor detenido.")
        return

    logger.info("Encontrados %d leads para reactivar.", total)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

    # Iterar en lotes controlados
    for idx in range(0, total, BATCH_SIZE):
        lote = leads[idx: idx + BATCH_SIZE]
        numero_lote = (idx // BATCH_SIZE) + 1
        logger.info("Lanzando lote %d (%d leads)...", numero_lote, len(lote))

        await asyncio.gather(
            *[_despachar_lead(lead, semaphore) for lead in lote],
            return_exceptions=True,  # Un error en uno no cancela los demás
        )

        # Pausa entre lotes para respetar rate limit de Meta
        if idx + BATCH_SIZE < total:
            logger.info(
                "Lote %d completado. Enfriando %ss antes del siguiente...",
                numero_lote, BATCH_DELAY_SECONDS,
            )
            await asyncio.sleep(BATCH_DELAY_SECONDS)

    logger.info("═══ Campaña de reactivación finalizada: %d leads procesados. ═══", total)


# ─── Punto de entrada (Docker / cron / manual) ────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(arrancar_motor_reactivacion())
    except KeyboardInterrupt:
        logger.warning("Motor detenido por el operador.")
