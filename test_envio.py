"""
test_envio.py — Script de verificación de envío HSM a producción.
Ejecutar desde la raíz del proyecto:

    python test_envio.py

Requiere que el archivo .env esté configurado con:
    META_ACCESS_TOKEN=EAAxx...
    META_PHONE_NUMBER_ID=12345678901234
"""
import asyncio
import logging
import os

# Carga el .env ANTES de importar cualquier módulo interno
from dotenv import load_dotenv
load_dotenv()

# Ahora sí importamos el servicio
from app.services.whatsapp_service import enviar_plantilla_hsm

# Logger mínimo para la prueba
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TestEnvio")


async def main() -> None:
    # ── Configura aquí tu número de prueba ──────────────────────────────
    # Formato internacional sin "+" (Meta lo acepta de ambas formas)
    NUMERO_DESTINO = os.environ.get("TEST_PHONE_NUMBER", "+573XXXXXXXXX")

    # Plantilla estática V1 (sin variables — el cuerpo es fijo)
    PLANTILLA = "bienvenida_multiproducto_v1"

    logger.info("Iniciando prueba de envío HSM...")
    logger.info("  → Destino   : %s", NUMERO_DESTINO)
    logger.info("  → Plantilla : %s", PLANTILLA)
    logger.info("  → Variables : ninguna (plantilla estática)")
    logger.info("  → Idioma    : es_CO")
    logger.info("-" * 60)

    # ── Llamada a producción ─────────────────────────────────────────────
    resultado = await enviar_plantilla_hsm(
        numero_destino=NUMERO_DESTINO,
        nombre_plantilla=PLANTILLA,
        variables=None,   # Plantilla estática: sin {{1}}
        idioma="es_CO",
    )

    # ── Diagnóstico del resultado ────────────────────────────────────────
    logger.info("-" * 60)
    if resultado.get("exito"):
        logger.info("✅ ENVÍO EXITOSO")
        logger.info("   msg_id  : %s", resultado.get("msg_id"))
        logger.info("   Verifica en WhatsApp que el mensaje llegó correctamente.")
    else:
        logger.error("❌ ENVÍO FALLIDO")
        logger.error("   status_code : %s", resultado.get("status_code", "N/A"))
        logger.error("   error       : %s", resultado.get("error"))
        logger.error("")
        logger.error("   → Revisa que META_ACCESS_TOKEN no esté expirado.")
        logger.error("   → Revisa que la plantilla '%s' esté APROBADA en Meta BM.", PLANTILLA)
        logger.error("   → Revisa que el número '%s' esté en la lista de testers si aún usas modo sandbox.", NUMERO_DESTINO)


if __name__ == "__main__":
    asyncio.run(main())
