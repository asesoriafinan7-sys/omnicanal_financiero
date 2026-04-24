"""
marketing_ads.py — Módulo de Meta Ads API para campañas de neuromarketing.

Capacidades:
  • Crear Campaña → AdSet → Ad completo con creativos.
  • Segmentación por sector económico con intereses y demografía.
  • Generación dinámica de ganchos comerciales por sector/producto.
  • Consulta de métricas y actualización de presupuesto en tiempo real.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from app.core.config import get_settings
from app.models.schemas import (
    ParametrosCampana,
    ProductoFinanciero,
    ResultadoCampana,
    SectorEconomico,
)
from app.services.ai_engine import generar_gancho_comercial

logger = logging.getLogger(__name__)
_settings = get_settings()

# ─── Constantes Meta Ads API ──────────────────────────────────────────────────
META_GRAPH_BASE = f"https://graph.facebook.com/{_settings.meta_api_version}"

# ─── Intereses de Meta Ads por sector económico (IDs reales de la plataforma) ─
# Nota: Los IDs deben verificarse en el Ad Manager para la región CO.
# Se usan los más relevantes disponibles por sector.
_INTERESES_POR_SECTOR: Dict[str, List[Dict[str, Any]]] = {
    "SALUD": [
        {"id": "6003348604166", "name": "Healthcare"},
        {"id": "6003107902433", "name": "Medicine"},
        {"id": "6003386330712", "name": "Nursing"},
    ],
    "EDUCACION": [
        {"id": "6003139266461", "name": "Education"},
        {"id": "6002925969972", "name": "Teaching"},
        {"id": "6003384571119", "name": "University"},
    ],
    "FUERZAS_MILITARES": [
        {"id": "6003349442579", "name": "Military"},
        {"id": "6003017857493", "name": "Defense"},
    ],
    "POLICIA_NACIONAL": [
        {"id": "6012381139513", "name": "Law enforcement"},
        {"id": "6003349442579", "name": "Military"},
    ],
    "GOBIERNO": [
        {"id": "6003006490081", "name": "Government"},
        {"id": "6003091644534", "name": "Public administration"},
    ],
    "EMPRESAS_PRIVADAS": [
        {"id": "6003257318413", "name": "Business"},
        {"id": "6003107902433", "name": "Finance"},
        {"id": "6002966523513", "name": "Entrepreneurship"},
    ],
    "PENSIONADOS": [
        {"id": "6002964291765", "name": "Retirement"},
        {"id": "6004023785773", "name": "Senior citizens"},
    ],
    "INDEPENDIENTES": [
        {"id": "6002966523513", "name": "Entrepreneurship"},
        {"id": "6003284416963", "name": "Freelance"},
        {"id": "6003006490081", "name": "Small business"},
    ],
    "SECTOR_ENERGETICO": [
        {"id": "6003009281961", "name": "Energy industry"},
        {"id": "6003201626862", "name": "Renewable energy"},
    ],
    "SECTOR_PETROLERO": [
        {"id": "6003015389073", "name": "Oil and gas industry"},
        {"id": "6003009281961", "name": "Energy"},
    ],
    "SECTOR_MINERO": [
        {"id": "6003015389073", "name": "Mining industry"},
        {"id": "6003006490081", "name": "Industry"},
    ],
    "SECTOR_FINANCIERO": [
        {"id": "6003107902433", "name": "Finance"},
        {"id": "6002956842769", "name": "Investment"},
        {"id": "6003186395972", "name": "Banking"},
    ],
    "SECTOR_TECNOLOGIA": [
        {"id": "6003596146169", "name": "Technology"},
        {"id": "6003348497699", "name": "Software"},
        {"id": "6003291634963", "name": "Computing"},
    ],
    "SECTOR_CONSTRUCCION": [
        {"id": "6003017857493", "name": "Construction"},
        {"id": "6003125461547", "name": "Real estate"},
    ],
    "SECTOR_AGROPECUARIO": [
        {"id": "6003007377552", "name": "Agriculture"},
        {"id": "6003348714163", "name": "Farming"},
    ],
    "DESCONOCIDO": [
        {"id": "6003107902433", "name": "Finance"},
        {"id": "6002956842769", "name": "Investment"},
    ],
}

# ─── Textos de anuncio por producto (neuromarketing) ─────────────────────────
_COPY_ANUNCIO: Dict[str, Dict[str, str]] = {
    "LIBRANZA": {
        "headline": "Libranza con la mejor tasa de Colombia 🏆",
        "body": (
            "Accede a tu libranza con descuento directo de nómina. "
            "Hasta 120 meses plazo, sin importar centrales de riesgo. "
            "¡Aprobación en 24 horas!"
        ),
        "cta": "SIGN_UP",
    },
    "CONSUMO": {
        "headline": "Crédito de consumo con desembolso en 48h 💸",
        "body": (
            "¿Necesitas liquidez ya? Crédito de consumo flexible, sin fiador. "
            "Tasa preferencial para empleados formales. Solicita ahora."
        ),
        "cta": "APPLY_NOW",
    },
    "COMPRA_CARTERA": {
        "headline": "Reduce hasta 40% tus cuotas mensuales 📉",
        "body": (
            "Unifica tus deudas con nuestra compra de cartera. "
            "Paga menos cada mes y ahorra en intereses. "
            "Consultamos sin costo y sin compromiso."
        ),
        "cta": "LEARN_MORE",
    },
    "MICROFINANZAS": {
        "headline": "Microcrédito desde $2M para impulsar tu negocio 🚀",
        "body": (
            "Financia tu negocio o proyecto personal desde $2.000.000 COP. "
            "Sin tanto papeleo, con acompañamiento financiero incluido."
        ),
        "cta": "GET_QUOTE",
    },
    "DESCONOCIDO": {
        "headline": "El crédito que necesitas, con la tasa que mereces 💰",
        "body": "Libranzas, consumo y compra de cartera. Consulta gratis y sin compromiso.",
        "cta": "LEARN_MORE",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# META ADS CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class MetaAdsClient:
    """
    Cliente para Meta Ads Marketing API.
    Maneja el ciclo completo: Campaña → AdSet → Creativo → Ad.
    """

    def __init__(self) -> None:
        self._token = _settings.meta_ads_access_token
        self._account_id = _settings.meta_ads_account_id
        if not self._token or not self._account_id:
            raise ValueError(
                "META_ADS_ACCESS_TOKEN y META_ADS_ACCOUNT_ID son requeridos."
            )

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{META_GRAPH_BASE}/{endpoint}"
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{META_GRAPH_BASE}/{endpoint}"
        params = params or {}
        params["access_token"] = self._token
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ── 1. CREAR CAMPAÑA ─────────────────────────────────────────────────────

    def crear_campana(self, nombre: str, objetivo: str = "LEAD_GENERATION") -> str:
        """Crea una campaña y retorna su ID."""
        payload = {
            "name": nombre,
            "objective": objetivo,
            "status": "PAUSED",  # Iniciamos pausada para revisión
            "special_ad_categories": [],
            "access_token": self._token,
        }
        data = self._post(f"act_{self._account_id}/campaigns", payload)
        campaign_id = data["id"]
        logger.info("Campaña creada: %s → ID: %s", nombre, campaign_id)
        return campaign_id

    # ── 2. CREAR ADSET CON SEGMENTACIÓN ──────────────────────────────────────

    def crear_adset(
        self,
        campaign_id: str,
        params: ParametrosCampana,
        nombre_adset: str,
    ) -> str:
        """Crea un AdSet con segmentación demográfica y por intereses."""
        intereses = _INTERESES_POR_SECTOR.get(
            params.sector_objetivo.value, _INTERESES_POR_SECTOR["DESCONOCIDO"]
        )
        if params.intereses_ids:
            intereses.extend([{"id": i} for i in params.intereses_ids])

        targeting: Dict[str, Any] = {
            "geo_locations": {
                "countries": params.ubicaciones_geo,
                "location_types": ["home", "recent"],
            },
            "age_min": params.rango_edad_min,
            "age_max": params.rango_edad_max,
            "interests": intereses,
            "publisher_platforms": ["facebook", "instagram", "audience_network"],
            "facebook_positions": ["feed", "story", "reels"],
            "instagram_positions": ["stream", "story", "reels"],
        }

        if params.genero == "MALE":
            targeting["genders"] = [1]
        elif params.genero == "FEMALE":
            targeting["genders"] = [2]

        payload = {
            "name": nombre_adset,
            "campaign_id": campaign_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "LEAD_GENERATION",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "daily_budget": int(params.presupuesto_diario_cop),  # ya en centavos
            "targeting": targeting,
            "start_time": params.fecha_inicio,
            "status": "PAUSED",
            "access_token": self._token,
        }
        if params.fecha_fin:
            payload["end_time"] = params.fecha_fin

        data = self._post(f"act_{self._account_id}/adsets", payload)
        adset_id = data["id"]
        logger.info("AdSet creado: %s → ID: %s", nombre_adset, adset_id)
        return adset_id

    # ── 3. CREAR CREATIVO ─────────────────────────────────────────────────────

    def crear_creativo(
        self,
        params: ParametrosCampana,
        page_id: Optional[str] = None,
    ) -> str:
        """Crea un creativo de anuncio con copy dinámico por sector y producto."""
        copy = _COPY_ANUNCIO.get(params.producto_financiero.value, _COPY_ANUNCIO["DESCONOCIDO"])
        gancho = params.gancho_comercial or generar_gancho_comercial(
            params.sector_objetivo, params.producto_financiero
        )

        object_story_spec: Dict[str, Any] = {
            "link_data": {
                "message": gancho,
                "description": params.texto_anuncio or copy["body"],
                "name": copy["headline"],
                "call_to_action": {"type": copy["cta"]},
            }
        }

        if params.imagen_creativo_url:
            object_story_spec["link_data"]["picture"] = params.imagen_creativo_url

        if page_id:
            object_story_spec["page_id"] = page_id

        payload = {
            "name": f"Creativo_{params.nombre}_{params.sector_objetivo.value}",
            "object_story_spec": object_story_spec,
            "access_token": self._token,
        }

        data = self._post(f"act_{self._account_id}/adcreatives", payload)
        creative_id = data["id"]
        logger.info("Creativo generado: ID %s", creative_id)
        return creative_id

    # ── 4. CREAR ANUNCIO ──────────────────────────────────────────────────────

    def crear_anuncio(
        self, adset_id: str, creative_id: str, nombre: str
    ) -> str:
        """Combina AdSet + Creativo en un Ad final."""
        payload = {
            "name": nombre,
            "adset_id": adset_id,
            "creative": {"creative_id": creative_id},
            "status": "PAUSED",
            "access_token": self._token,
        }
        data = self._post(f"act_{self._account_id}/ads", payload)
        ad_id = data["id"]
        logger.info("Anuncio creado: %s → ID: %s", nombre, ad_id)
        return ad_id

    # ── 5. FLUJO COMPLETO ─────────────────────────────────────────────────────

    def lanzar_campana_completa(
        self, params: ParametrosCampana, page_id: Optional[str] = None
    ) -> ResultadoCampana:
        """
        Orquesta el flujo completo:
        Campaña → AdSet → Creativo → Ad.
        Retorna ResultadoCampana con todos los IDs.
        """
        try:
            campaign_id = self.crear_campana(params.nombre, params.objetivo)
            adset_id = self.crear_adset(
                campaign_id, params, f"{params.nombre}_AS_{params.sector_objetivo.value}"
            )
            creative_id = self.crear_creativo(params, page_id)
            ad_id = self.crear_anuncio(
                adset_id, creative_id, f"{params.nombre}_AD_{params.producto_financiero.value}"
            )
            return ResultadoCampana(
                exito=True,
                campaign_id=campaign_id,
                adset_id=adset_id,
                ad_id=ad_id,
                mensaje=f"Campaña '{params.nombre}' creada y lista para activar.",
                datos_raw={
                    "sector": params.sector_objetivo.value,
                    "producto": params.producto_financiero.value,
                    "presupuesto_diario_cop": params.presupuesto_diario_cop,
                },
            )
        except requests.HTTPError as exc:
            logger.error("Meta Ads API HTTP error: %s — %s", exc, exc.response.text)
            return ResultadoCampana(
                exito=False,
                mensaje=f"Error API Meta Ads: {exc.response.text}",
            )
        except Exception as exc:
            logger.error("Error inesperado Meta Ads: %s", exc, exc_info=True)
            return ResultadoCampana(exito=False, mensaje=str(exc))

    # ── 6. MÉTRICAS DE CAMPAÑA ────────────────────────────────────────────────

    def obtener_metricas_campana(
        self,
        campaign_id: str,
        fields: Optional[List[str]] = None,
        fecha_inicio: str = "2024-01-01",
        fecha_fin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Consulta métricas de performance de una campaña."""
        f = fields or [
            "impressions", "reach", "clicks", "spend",
            "leads", "cpm", "cpc", "ctr", "cpp",
        ]
        params: Dict[str, Any] = {
            "fields": ",".join(f),
            "time_range": {"since": fecha_inicio},
        }
        if fecha_fin:
            params["time_range"]["until"] = fecha_fin

        try:
            data = self._get(f"{campaign_id}/insights", params)
            return data.get("data", [{}])[0] if data.get("data") else {}
        except Exception as exc:
            logger.error("Error obteniendo métricas: %s", exc)
            return {}

    # ── 7. ACTUALIZAR PRESUPUESTO ─────────────────────────────────────────────

    def actualizar_presupuesto_adset(
        self, adset_id: str, nuevo_presupuesto_cop: float
    ) -> bool:
        """Actualiza el presupuesto diario de un AdSet en tiempo real."""
        try:
            payload = {
                "daily_budget": int(nuevo_presupuesto_cop * 100),  # centavos
                "access_token": self._token,
            }
            resp = requests.post(
                f"{META_GRAPH_BASE}/{adset_id}",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Presupuesto AdSet %s actualizado a $%,.0f COP", adset_id, nuevo_presupuesto_cop)
            return True
        except Exception as exc:
            logger.error("Error actualizando presupuesto: %s", exc)
            return False

    # ── 8. ACTIVAR / PAUSAR CAMPAÑA ───────────────────────────────────────────

    def cambiar_estado_campana(
        self, campaign_id: str, estado: str = "ACTIVE"
    ) -> bool:
        """Activa o pausa una campaña. estado: 'ACTIVE' | 'PAUSED'"""
        try:
            payload = {"status": estado, "access_token": self._token}
            resp = requests.post(
                f"{META_GRAPH_BASE}/{campaign_id}",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Campaña %s → estado: %s", campaign_id, estado)
            return True
        except Exception as exc:
            logger.error("Error cambiando estado de campaña: %s", exc)
            return False

    # ── 9. LISTA DE CAMPAÑAS ACTIVAS ──────────────────────────────────────────

    def listar_campanas(self, estado: str = "ACTIVE") -> List[Dict[str, Any]]:
        """Lista todas las campañas de la cuenta ads con métricas básicas."""
        try:
            data = self._get(
                f"act_{self._account_id}/campaigns",
                {
                    "fields": "id,name,status,objective,daily_budget,start_time,stop_time",
                    "filtering": f'[{{"field":"effective_status","operator":"IN","value":["{estado}"]}}]',
                    "limit": 100,
                },
            )
            return data.get("data", [])
        except Exception as exc:
            logger.error("Error listando campañas: %s", exc)
            return []


# ─── Función de conveniencia para el endpoint FastAPI ────────────────────────

async def lanzar_campana_neuromarketing(
    params: ParametrosCampana,
    page_id: Optional[str] = None,
) -> ResultadoCampana:
    """Wrapper asíncrono para lanzar una campaña completa desde la API."""
    # En producción, Meta Ads API es síncrona; wrapeamos en executor si es necesario
    import asyncio

    loop = asyncio.get_event_loop()
    client = MetaAdsClient()
    return await loop.run_in_executor(None, client.lanzar_campana_completa, params, page_id)
