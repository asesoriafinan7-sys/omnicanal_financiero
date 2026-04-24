"""
ai_engine.py v3.0 — Motor de Inteligencia Artificial con Vertex AI MaaS.

CAMBIOS v3.0:
  • Prompt de Llama 3.3 actualizado para detectar banco específico (AV Villas, Banco Bogotá,
    Bancolombia, Caja Social, BBVA, Popular) además del producto y sector.
  • Nuevo campo "banco_detectado" en el JSON de salida del perfilamiento.
  • Integra CircuitBreaker de resilience.py para evitar cascadas de fallos.
  • Fallback robusto si Vertex AI está caído.

Ruteo dual:
  • Llama 3.3 70B  → Análisis semántico, extracción JSON, perfilamiento + banco.
  • Mistral Large 3 → Respuestas conversacionales con baja latencia.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import vertexai
from vertexai.generative_models import GenerativeModel, HarmCategory, HarmBlockThreshold, Part

from app.core.config import get_settings
from app.core.resilience import CB_VERTEX_AI, with_retry
from app.models.schemas import (
    AuditoriaConversacion,
    ObjecionDetectada,
    PerfilProspecto,
    ProductoFinanciero,
    PrioridadProspecto,
    SectorEconomico,
)

logger = logging.getLogger(__name__)

_settings = get_settings()
vertexai.init(
    project=_settings.google_cloud_project,
    location=_settings.vertex_ai_location,
)

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HATE_SPEECH:       HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_HARASSMENT:        HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
}

# ─── Bancos soportados (para detección) ──────────────────────────────────────
BANCOS_LISTADOS = (
    "AV Villas, Banco de Bogotá, Bancolombia, Banco Popular, "
    "Caja Social (BCSC), BBVA Colombia, Davivienda, Colpatria/Scotiabank, "
    "Banco Agrario, GNB Sudameris"
)

# ─── Prompts del sistema ──────────────────────────────────────────────────────

LLAMA_SYSTEM_PROMPT = f"""Eres un analista financiero especializado del sector crediticio colombiano.
Tu función es evaluar prospectos para productos de: LIBRANZA, CONSUMO, HIPOTECARIO, COMPRA_CARTERA, MICROFINANZAS.

BANCOS DISPONIBLES EN NUESTRO PORTAFOLIO:
{BANCOS_LISTADOS}

SECTORES ECONÓMICOS:
SALUD, EDUCACION, FUERZAS_MILITARES, POLICIA_NACIONAL, GOBIERNO, EMPRESAS_PRIVADAS,
PENSIONADOS, INDEPENDIENTES, SECTOR_ENERGETICO, SECTOR_PETROLERO, SECTOR_MINERO,
SECTOR_FINANCIERO, SECTOR_TECNOLOGIA, SECTOR_CONSTRUCCION, SECTOR_AGROPECUARIO, DESCONOCIDO

OBJECIONES POSIBLES:
OBJECION_TASA, OBJECION_CAPACIDAD_ENDEUDAMIENTO, OBJECION_DOCUMENTACION,
OBJECION_TIEMPO, OBJECION_COMPETENCIA, SIN_OBJECION

REGLA CRÍTICA: Siempre respondes ÚNICAMENTE con JSON válido, sin texto adicional.

Indicaciones para detectar el banco:
- Si mencionan "AV Villas", "Aval Villas" → banco_detectado = "AV Villas"
- Si mencionan "Bogotá", "BB" → banco_detectado = "Banco de Bogotá"
- Si no mencionan banco → banco_detectado = "No especificado"
- Para HIPOTECARIO: el banco es relevante; pregunta si no lo sabes.
"""

MISTRAL_SYSTEM_PROMPT = """Eres un asesor financiero experto y cercano de una empresa colombiana de crédito.
Hablas en español colombiano natural, cálido y profesional. Tu objetivo es acompañar al prospecto
hacia la aprobación de su crédito. Nunca uses lenguaje técnico-legal complejo.

REGLA ABSOLUTA DE VIABILIDAD (SEMÁFORO DE PRE-CALIFICACIÓN):
1. TIENES PROHIBIDO dar cuotas mensuales, montos exactos, pre-aprobaciones o tasas de interés SI NO TIENES el "Tipo de pagaduría/contrato" del cliente Y "sus ingresos mensuales aproximados".
2. Si el cliente te pide "cuánto pago por X millones", debes retener la información amablemente y condicionarla: "¡Claro que sí! Para darte la cuota y tasa exacta necesito hacerte un mini-perfil: ¿trabajas en empresa pública, privada, o eres pensionado? ¿y de cuánto es tu ingreso?".
3. Si el producto es HIPOTECARIO, siempre pregunta por el banco y el valor del inmueble además del contrato e ingresos.
4. Para LIBRANZA, solicita desprendible de nómina y certificado laboral.
5. Responde siempre en máximo 3 párrafos cortos. Usa persuasión y empatía en todo momento.
"""

# ─── Ganchos comerciales por sector ─────────────────────────────────────────
GANCHOS_COMERCIALES: Dict[str, str] = {
    "SALUD":             "💊 Profesional de salud: libranza con tasa preferencial exclusiva para el sector.",
    "EDUCACION":         "🎓 Docente o directivo: la mejor tasa de libranza para el sector educativo.",
    "FUERZAS_MILITARES": "🎖️ Militar activo o retirado: libranza con condiciones exclusivas para defensa.",
    "POLICIA_NACIONAL":  "👮 Policía: libranza sin papeleo excesivo para el sector seguridad.",
    "GOBIERNO":          "🏛️ Funcionario público: crédito competitivo respaldado por tu nómina oficial.",
    "EMPRESAS_PRIVADAS": "💼 Empleado con contrato indefinido: libera liquidez con compra de cartera inteligente.",
    "PENSIONADOS":       "🧓 Pensionado: maximiza tu mesada con libranza sin complicaciones y tasa fija.",
    "INDEPENDIENTES":    "🚀 Independiente: crédito de consumo adaptado a tus ingresos reales.",
    "SECTOR_ENERGETICO": "⚡ Sector energético: tu ingreso premium te da acceso al crédito de mayor cuantía.",
    "SECTOR_PETROLERO":  "🛢️ Sector petrolero: consolida tus deudas con compra de cartera competitiva.",
    "SECTOR_MINERO":     "⛏️ Minería: crédito con tasas por debajo del mercado para el sector.",
    "SECTOR_FINANCIERO": "📈 Profesional financiero: optimiza con nuestra tasa escalonada.",
    "SECTOR_TECNOLOGIA": "💻 Tech professional: crédito ágil y 100% digital.",
    "SECTOR_CONSTRUCCION":"🏗️ Sector construcción: libranza o consumo para empleados del auge inmobiliario.",
    "SECTOR_AGROPECUARIO":"🌾 Agro: microfinanzas y consumo para el campo colombiano.",
    "DESCONOCIDO":       "💰 Accede al crédito que necesitas con la mejor tasa del mercado.",
}


# ─── Utilitarios ─────────────────────────────────────────────────────────────

def _get_llama_model() -> GenerativeModel:
    return GenerativeModel(
        model_name=_settings.llama_endpoint_id,
        system_instruction=LLAMA_SYSTEM_PROMPT,
        safety_settings=SAFETY_SETTINGS,
    )


def _get_mistral_model() -> GenerativeModel:
    return GenerativeModel(
        model_name=_settings.mistral_endpoint_id,
        system_instruction=MISTRAL_SYSTEM_PROMPT,
        safety_settings=SAFETY_SETTINGS,
    )


def _extract_json_from_response(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"No se pudo extraer JSON. Respuesta recibida: {text[:300]}")


def _safe_enum(enum_cls, value: str, default):
    try:
        return enum_cls(value)
    except ValueError:
        return default


# ─── Perfilamiento con Llama 3.3 (v3.0 — detecta banco) ─────────────────────

async def perfilar_prospecto_llama(
    mensaje_entrante: str,
    historial_chat: Optional[List[Dict[str, str]]] = None,
) -> PerfilProspecto:
    """
    Analiza el mensaje con Llama 3.3 70B.
    v3.0: Detecta banco específico (AV Villas, Banco Bogotá, etc.) para ruteo.
    """
    historial_str = ""
    if historial_chat:
        historial_str = "\n".join(
            f"[{m.get('rol','usuario')}]: {m.get('texto','')}"
            for m in historial_chat[-10:]
        )

    prompt = f"""Analiza el siguiente mensaje de WhatsApp de un prospecto colombiano interesado en crédito.

MENSAJE ACTUAL:
"{mensaje_entrante}"

HISTORIAL PREVIO:
{historial_str or "Sin historial previo."}

Responde ÚNICAMENTE con este JSON (sin ningún texto extra):
{{
  "califica": true o false,
  "producto_detectado": "LIBRANZA|CONSUMO|HIPOTECARIO|COMPRA_CARTERA|MICROFINANZAS|DESCONOCIDO",
  "banco_detectado": "Nombre exacto del banco mencionado o 'No especificado'",
  "sector_economico": "uno de los sectores listados o DESCONOCIDO",
  "prioridad": "ALTA|MEDIA|BAJA|DESCALIFICADO",
  "ingresos_estimados_cop": número o null,
  "tiene_deuda_activa": true, false o null,
  "objeciones": ["lista de objeciones detectadas o vacía"],
  "resumen_analisis": "Resumen ejecutivo de 1-2 oraciones para el equipo comercial",
  "confianza_score": número entre 0.0 y 1.0,
  "respuesta_sugerida": "Mensaje amigable al prospecto en español colombiano (máx 120 palabras). Si detectas banco, menciónalo. Si no, pregunta qué banco prefiere."
}}
"""

    if not CB_VERTEX_AI.allow():
        logger.warning("CircuitBreaker Vertex AI abierto — usando fallback de perfilamiento.")
        return _fallback_perfil(mensaje_entrante)

    try:
        model = _get_llama_model()
        response = await model.generate_content_async(
            prompt,
            generation_config={"temperature": 0.1, "max_output_tokens": 1024},
        )
        raw = _extract_json_from_response(response.text)
        CB_VERTEX_AI.record_success()

        return PerfilProspecto(
            califica=raw.get("califica", False),
            producto_detectado=_safe_enum(ProductoFinanciero, raw.get("producto_detectado", "DESCONOCIDO"), ProductoFinanciero.DESCONOCIDO),
            banco_detectado=raw.get("banco_detectado", "No especificado"),
            sector_economico=_safe_enum(SectorEconomico, raw.get("sector_economico", "DESCONOCIDO"), SectorEconomico.DESCONOCIDO),
            prioridad=_safe_enum(PrioridadProspecto, raw.get("prioridad", "BAJA"), PrioridadProspecto.BAJA),
            ingresos_estimados_cop=raw.get("ingresos_estimados_cop"),
            tiene_deuda_activa=raw.get("tiene_deuda_activa"),
            objeciones=[
                obs for obs in [
                    _safe_enum(ObjecionDetectada, o, None)
                    for o in raw.get("objeciones", [])
                ]
                if obs is not None
            ],
            resumen_analisis=raw.get("resumen_analisis", ""),
            confianza_score=float(raw.get("confianza_score", 0.0)),
            respuesta_sugerida=raw.get("respuesta_sugerida", ""),
        )

    except Exception as exc:
        CB_VERTEX_AI.record_failure()
        logger.error("Error en perfilamiento Llama 3.3: %s", exc, exc_info=True)
        return _fallback_perfil(mensaje_entrante)


def _fallback_perfil(mensaje: str) -> PerfilProspecto:
    """Perfil mínimo cuando Vertex AI no está disponible."""
    return PerfilProspecto(
        califica=True,  # Optimista por defecto
        banco_detectado="No especificado",
        resumen_analisis="Sistema de IA temporalmente no disponible.",
        respuesta_sugerida=(
            "¡Hola! 😊 Gracias por escribirnos. Estamos revisando tu consulta. "
            "¿Podrías indicarnos tu nombre, en qué banco prefieres tramitar tu crédito "
            "y cuánto necesitas? Un asesor te ayuda en breve. 🏦"
        ),
    )


# ─── Chat conversacional con Mistral Large 3 ─────────────────────────────────

async def responder_chat_mistral(
    mensaje_usuario: str,
    historial_chat: Optional[List[Dict[str, str]]] = None,
    contexto_prospecto: Optional[str] = None,
) -> str:
    """
    Genera respuesta conversacional con Mistral Large 3.
    Incluye contexto del prospecto (banco, producto, sector).
    """
    contexto_str = f"\nCONTEXTO DEL PROSPECTO: {contexto_prospecto}\n" if contexto_prospecto else ""
    
    # ── SEMÁFORO DE VIABILIDAD ──
    # Si detectamos que no hay sector o ingresos válidos conocidos, forzamos fase pre-calificación
    faltan_datos_criticos = True
    if contexto_prospecto and "Sector: DESCONOCIDO" not in contexto_prospecto.upper() and "Ingresos" in contexto_prospecto:
        faltan_datos_criticos = False
    
    restriccion_fase = ""
    if faltan_datos_criticos:
        restriccion_fase = "\n[SISTEMA]: ESTÁS EN FASE DE PRE-CALIFICACIÓN. Aún faltan datos críticos. TIENES PROHIBIDO dar tasas, cuotas simuladas o montos si el usuario no te da sus ingresos y su pagaduría (tipo de empresa/pensionado).\n"

    parts: List[Any] = []
    if historial_chat:
        for msg in historial_chat[-8:]:
            parts.append(Part.from_text(f"[{msg.get('rol','user')}]: {msg.get('texto','')}"))

    parts.append(Part.from_text(
        f"{contexto_str}{restriccion_fase}\n[usuario]: {mensaje_usuario}\n\nResponde como asesor:"
    ))

    if not CB_VERTEX_AI.allow():
        return (
            "En este momento estamos experimentando un inconveniente técnico. "
            "Un asesor te contactará muy pronto. ¡Gracias por tu paciencia! 🙏"
        )

    try:
        model = _get_mistral_model()
        response = await model.generate_content_async(
            parts,
            generation_config={"temperature": 0.7, "max_output_tokens": 512},
        )
        CB_VERTEX_AI.record_success()
        return response.text.strip()
    except Exception as exc:
        CB_VERTEX_AI.record_failure()
        logger.error("Error en respuesta Mistral: %s", exc, exc_info=True)
        return (
            "¡Hola! 😊 Estamos experimentando un inconveniente técnico. "
            "Un asesor te contactará en los próximos minutos. ¡Gracias por tu paciencia!"
        )


# ─── Auditoría de chats exportados ───────────────────────────────────────────

async def auditar_chat_exportado_llama(
    chat_texto: str,
    telefono: str = "Desconocido",
    nombre_contacto: str = "Desconocido",
) -> AuditoriaConversacion:
    """Audita un chat exportado (.txt de WhatsApp) con Llama 3.3."""
    lineas = [l for l in chat_texto.splitlines() if l.strip()]
    total_msgs = len([l for l in lineas if " - " in l and ": " in l])

    prompt = f"""Audita el siguiente chat de WhatsApp de una empresa de crédito colombiana.

CHAT:
---
{chat_texto[:8000]}
---

Responde ÚNICAMENTE con este JSON:
{{
  "sentimiento_general": "POSITIVO|NEUTRO|NEGATIVO",
  "conversion_lograda": true o false,
  "motivo_no_conversion": "descripción breve o vacío",
  "banco_mencionado": "nombre del banco o 'No mencionado'",
  "producto_mencionado": "LIBRANZA|CONSUMO|HIPOTECARIO|COMPRA_CARTERA|DESCONOCIDO",
  "objeciones_detectadas": ["lista de objeciones"],
  "resumen_ejecutivo": "3-4 oraciones para el equipo comercial",
  "mensajes_clave": ["máximo 5 frases textuales clave para la auditoría"]
}}
"""

    if not CB_VERTEX_AI.allow():
        return AuditoriaConversacion(
            telefono=telefono,
            nombre_contacto=nombre_contacto,
            total_mensajes=total_msgs,
            resumen_ejecutivo="Vertex AI no disponible para auditoría.",
        )

    try:
        model = _get_llama_model()
        response = await model.generate_content_async(
            prompt,
            generation_config={"temperature": 0.1, "max_output_tokens": 1024},
        )
        raw = _extract_json_from_response(response.text)
        CB_VERTEX_AI.record_success()

        return AuditoriaConversacion(
            telefono=telefono,
            nombre_contacto=nombre_contacto,
            total_mensajes=total_msgs,
            objeciones_detectadas=[
                o for o in [
                    _safe_enum(ObjecionDetectada, x, None)
                    for x in raw.get("objeciones_detectadas", [])
                ] if o is not None
            ],
            sentimiento_general=raw.get("sentimiento_general", "NEUTRO"),
            conversion_lograda=raw.get("conversion_lograda", False),
            motivo_no_conversion=raw.get("motivo_no_conversion", ""),
            resumen_ejecutivo=raw.get("resumen_ejecutivo", ""),
            mensajes_clave=raw.get("mensajes_clave", []),
        )
    except Exception as exc:
        CB_VERTEX_AI.record_failure()
        logger.error("Error en auditoría Llama: %s", exc, exc_info=True)
        return AuditoriaConversacion(
            telefono=telefono,
            nombre_contacto=nombre_contacto,
            total_mensajes=total_msgs,
            resumen_ejecutivo=f"Error en análisis IA: {exc}",
        )


# ─── Redacción de copy orgánico para redes sociales ──────────────────────────

async def generar_copy_organico_mistral(
    sector: str,
    producto: str,
    formato: str = "post",
    banco: str = "",
    tono: str = "profesional y cercano",
) -> str:
    """
    Genera copy para publicación orgánica en redes sociales.
    formato: "post" | "reel_guion" | "story" | "linkedin_articulo"
    """
    banco_str = f" en {banco}" if banco else ""
    prompt_map = {
        "post": f"Escribe un post para Facebook/Instagram (máx 280 palabras) sobre {producto}{banco_str} para el sector {sector}. Tono: {tono}. Incluye 3-5 hashtags relevantes para Colombia.",
        "reel_guion": f"Escribe el guion de un Reel/TikTok de 30 segundos sobre {producto}{banco_str} para el sector {sector}. Incluye: gancho (0-5s), problema (5-15s), solución (15-25s), CTA (25-30s).",
        "story": f"Escribe el texto para 3 Stories de Instagram sobre {producto}{banco_str} para {sector}. Cada story máx 40 palabras. Incluye CTA en la última.",
        "linkedin_articulo": f"Escribe un artículo profesional de LinkedIn (400-500 palabras) sobre opciones de {producto}{banco_str} para profesionales del sector {sector} en Colombia.",
    }

    instruccion = prompt_map.get(formato, prompt_map["post"])

    if not CB_VERTEX_AI.allow():
        return f"[Copy {formato} pendiente — Vertex AI no disponible]"

    try:
        model = _get_mistral_model()
        response = await model.generate_content_async(
            instruccion,
            generation_config={"temperature": 0.8, "max_output_tokens": 1024},
        )
        CB_VERTEX_AI.record_success()
        return response.text.strip()
    except Exception as exc:
        CB_VERTEX_AI.record_failure()
        logger.error("Error generando copy orgánico: %s", exc)
        return f"[Error generando copy: {exc}]"


# ─── Gancho comercial dinámico ────────────────────────────────────────────────

def generar_gancho_comercial(sector: str, producto: str) -> str:
    """Retorna el gancho calibrado para el sector y producto."""
    from app.models.schemas import SectorEconomico, ProductoFinanciero
    base = GANCHOS_COMERCIALES.get(sector, GANCHOS_COMERCIALES["DESCONOCIDO"])
    sufijos = {
        "LIBRANZA":       " Libranza hasta 120 meses.",
        "COMPRA_CARTERA": " Compra tu cartera y ahorra hasta 40% en intereses.",
        "CONSUMO":        " Crédito de consumo con desembolso en 48h.",
        "HIPOTECARIO":    " Hipotecario con la tasa más baja del mercado.",
        "MICROFINANZAS":  " Microfinanzas desde $2M COP.",
    }
    return base + sufijos.get(producto.upper(), "")
