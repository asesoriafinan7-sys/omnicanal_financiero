"""
business_rules.py — Motor de Reglas de Negocio y Ruteo.

Lee y cachea business_rules.json. Decide:
  • A qué outsourcing va un prospecto (WiCapital / Expertos / Vivienda Total).
  • Qué banco aplica según el perfil.
  • Si el chat debe escalar a un humano.
  • Qué documentos solicitar.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).parent / "business_rules.json"

# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    outsourcing:         str              # "WICAPITAL" | "EXPERTOS" | "VIVIENDA_TOTAL"
    integracion:         str              # "scraper"   | "email"
    email_principal:     Optional[str]
    email_avanzado:      Optional[str]
    trigger_avanzado:    List[str]
    tasa_min:            Optional[float]
    tasa_max:            Optional[float]
    plazo_max_meses:     Optional[int]
    documentos:          List[str]
    banco_normalizado:   str


@dataclass
class EscaladaDecision:
    escalar: bool
    motivo:  str          # "intentos_fallidos" | "keyword" | ""
    mensaje: str


# ─── Normalización de nombre de banco ────────────────────────────────────────

_BANCO_ALIAS: Dict[str, str] = {
    # AV Villas
    "av villas": "AV_VILLAS", "avvillas": "AV_VILLAS", "av_villas": "AV_VILLAS",
    "aval villas": "AV_VILLAS",
    # Banco de Bogotá
    "banco de bogota": "BANCO_BOGOTA", "bogota": "BANCO_BOGOTA",
    "bco bogota": "BANCO_BOGOTA", "banco bogota": "BANCO_BOGOTA",
    # Bancolombia
    "bancolombia": "BANCOLOMBIA",
    # Banco Popular
    "banco popular": "BANCO_POPULAR", "popular": "BANCO_POPULAR",
    # Caja Social
    "caja social": "CAJA_SOCIAL", "bcsc": "CAJA_SOCIAL", "caja social bcsc": "CAJA_SOCIAL",
    # BBVA
    "bbva": "BBVA",
    # Davivienda
    "davivienda": "DAVIVIENDA",
    # Colpatria / Scotiabank
    "colpatria": "COLPATRIA", "scotiabank": "COLPATRIA",
    # Agrario
    "agrario": "BANCO_AGRARIO", "banco agrario": "BANCO_AGRARIO",
}


def normalizar_banco(banco_raw: str) -> str:
    """Normaliza el nombre del banco a la clave estándar usada en business_rules.json."""
    key = banco_raw.lower().strip()
    return _BANCO_ALIAS.get(key, "DEFAULT")


# ─── Motor de reglas ─────────────────────────────────────────────────────────

class BusinessRulesEngine:
    """
    Carga business_rules.json y expone métodos para tomar decisiones
    de ruteo, escalada y documentación. Recarga el JSON si detecta cambio.
    """

    def __init__(self) -> None:
        self._rules: Dict[str, Any] = {}
        self._mtime: float = 0.0
        self._load()

    def _load(self) -> None:
        """Carga o recarga las reglas si el archivo cambió."""
        try:
            mtime = _RULES_PATH.stat().st_mtime
            if mtime != self._mtime:
                with open(_RULES_PATH, "r", encoding="utf-8") as f:
                    self._rules = json.load(f)
                self._mtime = mtime
                logger.info("business_rules.json cargado/recargado.")
        except Exception as exc:
            logger.error("Error cargando business_rules.json: %s", exc)

    def _r(self) -> Dict[str, Any]:
        """Retorna las reglas, recargando si el archivo fue modificado."""
        self._load()
        return self._rules

    # ── Ruteo ─────────────────────────────────────────────────────────────────

    def get_routing(self, producto: str, banco_raw: str) -> RoutingDecision:
        """
        Retorna la decisión de ruteo completa para un producto y banco dados.

        Args:
            producto: "LIBRANZA" | "CONSUMO" | "HIPOTECARIO" | "COMPRA_CARTERA" | ...
            banco_raw: Nombre del banco tal como lo mencionó el prospecto.
        """
        rules = self._r()
        banco_key = normalizar_banco(banco_raw)

        matriz = rules.get("matriz_ruteo", {})
        prod_rules = matriz.get(producto.upper(), matriz.get("DESCONOCIDO", {}))

        # Buscar regla específica de banco, o DEFAULT
        regla: Dict[str, Any] = prod_rules.get(banco_key, prod_rules.get("DEFAULT", {
            "outsourcing": "WICAPITAL",
            "integracion": "scraper",
            "email_principal": None,
            "email_avanzado": None,
            "trigger_email_avanzado": [],
        }))

        # Obtener tasa del banco/producto
        tasas_banco = rules.get("tasas_vigentes", {}).get(banco_key, {})
        tasas_prod  = tasas_banco.get(producto.upper(), {})

        # Documentos requeridos
        docs_prod = rules.get("documentos_requeridos", {}).get(producto.upper(), {})
        documentos = (
            docs_prod.get(banco_key)
            or docs_prod.get("DEFAULT")
            or ["Cédula de ciudadanía", "Último desprendible de nómina"]
        )

        return RoutingDecision(
            outsourcing=regla.get("outsourcing", "WICAPITAL"),
            integracion=regla.get("integracion", "scraper"),
            email_principal=regla.get("email_principal"),
            email_avanzado=regla.get("email_avanzado"),
            trigger_avanzado=regla.get("trigger_email_avanzado", []),
            tasa_min=tasas_prod.get("min"),
            tasa_max=tasas_prod.get("max"),
            plazo_max_meses=tasas_prod.get("plazo_max_meses"),
            documentos=documentos,
            banco_normalizado=banco_key,
        )

    # ── Escalada a humano ─────────────────────────────────────────────────────

    def should_escalate(
        self,
        telefono: str,
        texto_mensaje: str,
        intentos_fallidos: int = 0,
    ) -> EscaladaDecision:
        """
        Determina si el chat debe escalar a un asesor humano.
        Verifica keywords de escalada e intentos fallidos.
        """
        rules = self._r()
        esc_cfg = rules.get("escalada_humano", {})
        max_intentos = esc_cfg.get("intentos_fallidos_max", 3)
        keywords     = esc_cfg.get("keywords_escalada", [])
        mensaje_esc  = esc_cfg.get(
            "mensaje_escalada",
            "Un asesor te atenderá en breve. ¡Gracias por tu paciencia!",
        )

        # Verificar keywords en el mensaje
        texto_lower = texto_mensaje.lower()
        for kw in keywords:
            if kw.lower() in texto_lower:
                return EscaladaDecision(
                    escalar=True,
                    motivo="keyword",
                    mensaje=mensaje_esc,
                )

        # Verificar intentos fallidos
        if intentos_fallidos >= max_intentos:
            return EscaladaDecision(
                escalar=True,
                motivo="intentos_fallidos",
                mensaje=mensaje_esc,
            )

        return EscaladaDecision(escalar=False, motivo="", mensaje="")

    # ── Documentos ────────────────────────────────────────────────────────────

    def get_documentos_requeridos(self, producto: str, banco_raw: str) -> List[str]:
        """Retorna la lista de documentos requeridos para un producto y banco."""
        return self.get_routing(producto, banco_raw).documentos

    def generar_mensaje_documentos(self, producto: str, banco_raw: str, nombre: str = "cliente") -> str:
        """Genera un mensaje amigable con los documentos necesarios."""
        docs = self.get_documentos_requeridos(producto, banco_raw)
        banco_display = banco_raw.title() if banco_raw != "DEFAULT" else "el banco seleccionado"
        lista = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(docs))
        return (
            f"¡Excelente {nombre}! 📋 Para avanzar con tu {producto.title()} en "
            f"{banco_display}, necesitamos los siguientes documentos:\n\n"
            f"{lista}\n\n"
            "Por favor envíanos los documentos en foto clara o PDF. "
            "¿Tienes alguno de estos a mano? 😊"
        )

    # ── Tasas ────────────────────────────────────────────────────────────────

    def get_oferta_tasa(self, producto: str, banco_raw: str) -> str:
        """Retorna un string con la tasa ofrecida según banco y producto."""
        routing = self.get_routing(producto, banco_raw)
        if routing.tasa_min and routing.tasa_max:
            return f"{routing.tasa_min:.2f}% a {routing.tasa_max:.2f}% E.M."
        if routing.tasa_min:
            return f"desde {routing.tasa_min:.2f}% E.M."
        return "tasa preferencial (consultamos para ti)"

    # ── WiCapital eventos críticos ────────────────────────────────────────────

    def is_critico_wicapital(self, seccion_anterior: str, seccion_nueva: str, estado_nuevo: str) -> dict:
        """
        Determina si el cambio en WiCapital es un evento crítico que requiere acción extra.
        Retorna {"critico": bool, "emoji": str, "tipo": "aprobado"|"rechazado"|"avance"|""}
        """
        rules = self._r()
        eventos = rules.get("wicapital_eventos_criticos", {})

        # Revisar transiciones importantes
        for trans in eventos.get("transiciones_alerta_alta", []):
            if trans["de"] == seccion_anterior and trans["a"] == seccion_nueva:
                return {"critico": True, "emoji": trans.get("emoji", "🔄"), "tipo": "avance"}

        # Estado rechazado
        for rechazado in eventos.get("estados_rechazados", []):
            if rechazado.lower() in estado_nuevo.lower():
                return {"critico": True, "emoji": "❌", "tipo": "rechazado"}

        # Estado aprobado
        for aprobado in eventos.get("estados_aprobados", []):
            if aprobado.lower() in estado_nuevo.lower():
                return {"critico": True, "emoji": "✅", "tipo": "aprobado"}

        return {"critico": False, "emoji": "🔄", "tipo": ""}

    def get_etapa_pipeline(self, seccion_wicapital: str) -> str:
        """Mapea una sección de WiCapital a la etapa del pipeline Kanban."""
        rules = self._r()
        mapeo = rules.get("pipeline_kanban", {}).get("mapeo_wicapital", {})
        return mapeo.get(seccion_wicapital, "Viabilidad")


# ─── Singleton ───────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_rules_engine() -> BusinessRulesEngine:
    """Singleton del motor de reglas."""
    return BusinessRulesEngine()
