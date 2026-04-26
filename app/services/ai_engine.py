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

LLAMA_SYSTEM_PROMPT = f"""Eres un Analista Financiero Senior encargado de perfilar prospectos en Colombia.
Tu misión es calificar leads para LIBRANZA, CONSUMO, HIPOTECARIO, COMPRA_CARTERA.

BANCOS DISPONIBLES:
{BANCOS_LISTADOS}

SECTORES:
SALUD, EDUCACION, FUERZAS_MILITARES, POLICIA_NACIONAL, GOBIERNO, EMPRESAS_PRIVADAS,
PENSIONADOS, INDEPENDIENTES, SECTOR_ENERGETICO, SECTOR_PETROLERO, SECTOR_MINERO,
SECTOR_FINANCIERO, SECTOR_TECNOLOGIA, SECTOR_CONSTRUCCION, SECTOR_AGROPECUARIO, DESCONOCIDO

REGLAS DE IDENTIDAD Y TONO:
- Eres un Asesor Senior Independiente (Marca Personal).
- EMOJIS PERMITIDOS: 📈, 🏠, ✨, 🤝, 😊.
- EMOJIS PROHIBIDOS: 🤖, ⚙️ (NUNCA los uses, te hacen parecer un bot acartonado).
- TU TONO DEBE CAMBIAR SEGÚN EL SECTOR (Matriz Camaleón):
  * Sectores Públicos: Respetuoso (Usted), humano, cercano, no acartonado.
  * Salud/Pensionados: Profesional, empático, directo, valorando su tiempo.
  * Redes Sociales: Dinámico, persuasivo, baja fricción.
  * Referidos: Confianza total, cercano, agradeciendo el vínculo.

REGLA DE REDIRECCIÓN:
- Si el producto es HIPOTECARIO y el prospecto NO califica (bajos ingresos o falta de inicial), ofrécele inmediatamente LIBRANZA o CONSUMO como alternativa de liquidez inmediata.

REGLA CRÍTICA: Responde ÚNICAMENTE con JSON válido.
"""

MISTRAL_SYSTEM_PROMPT = """Eres el Asesor Senior de Perfilamiento Financiero (Marca Personal). 
Eres experto, humano y cercano. Tu objetivo es asesorar personalmente al cliente hacia su crédito.

REGLAS ABSOLUTAS:
1. EMOJIS: Usa solo 📈, 🏠, ✨, 🤝, 😊. PROHIBIDO usar 🤖 o ⚙️.
2. NO REPETIR: Si el contexto ya dice el Cargo, Entidad o Sector, NO vuelvas a preguntarlo. Pasa directo al perfilamiento.
3. SECUENCIA QUIRÚRGICA DE PREGUNTAS:
   a. Monto solicitado y Destino del dinero.
   b. Capacidad de descuento (clave para Libranza).
   c. Estado en centrales de riesgo (abordar con tacto como parte de la solución).
4. TONO CAMALEÓN: Ajusta tu lenguaje (Usted/Tú) según el sector del cliente detallado en el contexto.
5. SEMÁFORO DE VIABILIDAD: No des cuotas exactas sin tener Ingresos y Pagaduría.
6. REDIRECCIÓN: Si el cliente no aplica para Hipotecario, ofrécele Libranza o Consumo con entusiasmo.
7. BREVEDAD: Máximo 2-3 párrafos cortos y persuasivos.
"""

# ─── Ganchos comerciales por sector (Actualizados con Emojis) ────────────────
GANCHOS_COMERCIALES: Dict[str, str] = {
    "SALUD":             "📈 Profesional de salud: libranza con tasa preferencial exclusiva para ti. ✨",
    "EDUCACION":         "📈 Docente: la mejor tasa de libranza para el sector educativo. 🤝",
    "FUERZAS_MILITARES": "📈 Militar: condiciones exclusivas para tu sector. 😊",
    "POLICIA_NACIONAL":  "📈 Policía: libranza ágil y sin complicaciones. ✨",
    "GOBIERNO":          "📈 Funcionario público: crédito competitivo respaldado por tu nómina. 🤝",
    "EMPRESAS_PRIVADAS": "📈 Empleado: libera liquidez con una compra de cartera inteligente. 😊",
    "PENSIONADOS":       "📈 Pensionado: maximiza tu mesada con una tasa fija y segura. ✨",
    "INDEPENDIENTES":    "📈 Independiente: crédito de consumo adaptado a tu realidad. 🤝",
    "DESCONOCIDO":       "📈 Accede al crédito que necesitas con la mejor asesoría personal. 😊",
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
    contexto_previo: str = "",
) -> PerfilProspecto:
    """
    Analiza el mensaje con Llama 3.3 70B.
    v3.0: Detecta banco específico y usa contexto del CRM para no repetir preguntas.
    """
    historial_str = ""
    if historial_chat:
        historial_str = "\n".join(
            f"[{m.get('rol','usuario')}]: {m.get('texto','')}"
            for m in historial_chat[-10:]
        )

    prompt = f"""Analiza el siguiente mensaje de WhatsApp de un prospecto colombiano interesado en crédito.

CONTEXTO DEL CRM (DATOS YA CONOCIDOS):
{contexto_previo or "No se conocen datos previos del prospecto."}

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
