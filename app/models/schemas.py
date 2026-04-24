"""
Esquemas Pydantic para el ecosistema omnicanal financiero.
Valida todos los datos de entrada/salida de la API y las entidades de dominio.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import re


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class ProductoFinanciero(str, Enum):
    LIBRANZA = "LIBRANZA"
    CONSUMO = "CONSUMO"
    COMPRA_CARTERA = "COMPRA_CARTERA"
    MICROFINANZAS = "MICROFINANZAS"
    DESCONOCIDO = "DESCONOCIDO"


class SectorEconomico(str, Enum):
    SALUD = "SALUD"
    EDUCACION = "EDUCACION"
    FUERZAS_MILITARES = "FUERZAS_MILITARES"
    POLICIA_NACIONAL = "POLICIA_NACIONAL"
    GOBIERNO = "GOBIERNO"
    EMPRESAS_PRIVADAS = "EMPRESAS_PRIVADAS"
    PENSIONADOS = "PENSIONADOS"
    INDEPENDIENTES = "INDEPENDIENTES"
    SECTOR_ENERGETICO = "SECTOR_ENERGETICO"
    SECTOR_PETROLERO = "SECTOR_PETROLERO"
    SECTOR_MINERO = "SECTOR_MINERO"
    SECTOR_FINANCIERO = "SECTOR_FINANCIERO"
    SECTOR_TECNOLOGIA = "SECTOR_TECNOLOGIA"
    SECTOR_CONSTRUCCION = "SECTOR_CONSTRUCCION"
    SECTOR_AGROPECUARIO = "SECTOR_AGROPECUARIO"
    DESCONOCIDO = "DESCONOCIDO"


class PrioridadProspecto(str, Enum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"
    DESCALIFICADO = "DESCALIFICADO"


class ObjecionDetectada(str, Enum):
    TASA = "OBJECION_TASA"
    CAPACIDAD_ENDEUDAMIENTO = "OBJECION_CAPACIDAD_ENDEUDAMIENTO"
    DOCUMENTACION = "OBJECION_DOCUMENTACION"
    TIEMPO = "OBJECION_TIEMPO"
    COMPETENCIA = "OBJECION_COMPETENCIA"
    NINGUNA = "SIN_OBJECION"


class EstadoWiCapital(str, Enum):
    FILTROS = "Gestión Filtros"
    RADICADOS = "Gestión Radicados"
    APROBADOS = "Gestión Aprobados"
    DESEMBOLSO = "Gestión Desembolso"


# ─────────────────────────────────────────────────────────────────────────────
# WHATSAPP WEBHOOK SCHEMAS (Meta Graph API)
# ─────────────────────────────────────────────────────────────────────────────

class WAProfile(BaseModel):
    name: str


class WAContact(BaseModel):
    profile: WAProfile
    wa_id: str


class WATextMessage(BaseModel):
    body: str

class WAInteractiveButtonReply(BaseModel):
    id: str
    title: Optional[str] = None

class WAInteractiveMessage(BaseModel):
    type: str
    button_reply: Optional[WAInteractiveButtonReply] = None


class WAMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    text: Optional[WATextMessage] = None
    interactive: Optional[WAInteractiveMessage] = None
    type: str

    model_config = {"populate_by_name": True}


class WAValue(BaseModel):
    messaging_product: str
    metadata: Dict[str, Any]
    contacts: Optional[List[WAContact]] = None
    messages: Optional[List[WAMessage]] = None
    statuses: Optional[List[Dict[str, Any]]] = None


class WAChange(BaseModel):
    value: WAValue
    field: str


class WAEntry(BaseModel):
    id: str
    changes: List[WAChange]


class WhatsAppWebhook(BaseModel):
    object: str
    entry: List[WAEntry]


# ─────────────────────────────────────────────────────────────────────────────
# PROSPECTO
# ─────────────────────────────────────────────────────────────────────────────

class PerfilProspecto(BaseModel):
    """Resultado del análisis de Llama 3.3 sobre un mensaje entrante."""
    califica: bool
    producto_detectado: ProductoFinanciero = ProductoFinanciero.DESCONOCIDO
    sector_economico: SectorEconomico = SectorEconomico.DESCONOCIDO
    prioridad: PrioridadProspecto = PrioridadProspecto.BAJA
    ingresos_estimados_cop: Optional[float] = None
    tiene_deuda_activa: Optional[bool] = None
    objeciones: List[ObjecionDetectada] = Field(default_factory=list)
    resumen_analisis: str = ""
    confianza_score: float = Field(default=0.0, ge=0.0, le=1.0)
    respuesta_sugerida: str = ""


class Prospecto(BaseModel):
    """Entidad principal de prospecto para CRM Google Sheets."""
    telefono: str
    nombre: str = "Desconocido"
    email: Optional[str] = None
    sector_economico: SectorEconomico = SectorEconomico.DESCONOCIDO
    producto_interes: ProductoFinanciero = ProductoFinanciero.DESCONOCIDO
    prioridad: PrioridadProspecto = PrioridadProspecto.BAJA
    estado_crm: str = "NUEVO"
    ingresos_estimados_cop: Optional[float] = None
    objeciones: List[str] = Field(default_factory=list)
    notas: str = ""
    fuente: str = "WHATSAPP_INBOUND"
    fecha_creacion: datetime = Field(default_factory=datetime.utcnow)
    fecha_ultimo_contacto: datetime = Field(default_factory=datetime.utcnow)
    campana_origen: Optional[str] = None

    @field_validator("telefono")
    @classmethod
    def normalizar_telefono(cls, v: str) -> str:
        """Normaliza a formato internacional colombiano +57XXXXXXXXXX."""
        digitos = re.sub(r"\D", "", v)
        if digitos.startswith("57") and len(digitos) == 12:
            return f"+{digitos}"
        if len(digitos) == 10 and digitos.startswith("3"):
            return f"+57{digitos}"
        if len(digitos) == 10 and digitos.startswith("6"):
            return f"+57{digitos}"
        return f"+{digitos}" if not v.startswith("+") else v

    def to_sheets_row(self) -> List[Any]:
        """Convierte a fila plana para Google Sheets."""
        return [
            self.telefono,
            self.nombre,
            self.email or "",
            self.sector_economico.value,
            self.producto_interes.value,
            self.prioridad.value,
            self.estado_crm,
            self.ingresos_estimados_cop or "",
            "; ".join(self.objeciones),
            self.notas,
            self.fuente,
            self.fecha_creacion.isoformat(),
            self.fecha_ultimo_contacto.isoformat(),
            self.campana_origen or "",
        ]

    @classmethod
    def sheets_headers(cls) -> List[str]:
        return [
            "Telefono", "Nombre", "Email", "Sector", "Producto", "Prioridad",
            "Estado_CRM", "Ingresos_COP", "Objeciones", "Notas", "Fuente",
            "Fecha_Creacion", "Ultimo_Contacto", "Campana_Origen",
        ]


# ─────────────────────────────────────────────────────────────────────────────
# AUDITORÍA DE CHAT
# ─────────────────────────────────────────────────────────────────────────────

class AuditoriaConversacion(BaseModel):
    """Resultado del diagnóstico de un chat exportado de WhatsApp."""
    telefono: str
    nombre_contacto: str = "Desconocido"
    total_mensajes: int = 0
    primer_mensaje: Optional[datetime] = None
    ultimo_mensaje: Optional[datetime] = None
    objeciones_detectadas: List[ObjecionDetectada] = Field(default_factory=list)
    sentimiento_general: str = "NEUTRO"  # POSITIVO, NEUTRO, NEGATIVO
    conversion_lograda: bool = False
    motivo_no_conversion: str = ""
    resumen_ejecutivo: str = ""
    mensajes_clave: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAÑAS META ADS
# ─────────────────────────────────────────────────────────────────────────────

class ParametrosCampana(BaseModel):
    """Parámetros para crear/actualizar una campaña en Meta Ads."""
    nombre: str
    objetivo: str = "LEAD_GENERATION"
    sector_objetivo: SectorEconomico
    producto_financiero: ProductoFinanciero
    presupuesto_diario_cop: float = Field(ge=10_000)
    fecha_inicio: str  # YYYY-MM-DD
    fecha_fin: Optional[str] = None
    gancho_comercial: str = ""
    ubicaciones_geo: List[str] = Field(default_factory=lambda: ["CO"])
    rango_edad_min: int = Field(default=25, ge=18)
    rango_edad_max: int = Field(default=60, le=65)
    genero: str = "ALL"  # ALL, MALE, FEMALE
    intereses_ids: List[str] = Field(default_factory=list)
    imagen_creativo_url: Optional[str] = None
    texto_anuncio: str = ""
    cta: str = "LEARN_MORE"

    @field_validator("presupuesto_diario_cop")
    @classmethod
    def convertir_a_centavos(cls, v: float) -> float:
        """Meta Ads requiere el presupuesto en centavos de la divisa."""
        return round(v * 100)  # COP a centavos


class ResultadoCampana(BaseModel):
    """Resultado de la operación sobre Meta Ads API."""
    exito: bool
    campaign_id: Optional[str] = None
    adset_id: Optional[str] = None
    ad_id: Optional[str] = None
    mensaje: str = ""
    datos_raw: Optional[Dict[str, Any]] = None


# ─────────────────────────────────────────────────────────────────────────────
# WICAPITAL CRM
# ─────────────────────────────────────────────────────────────────────────────

class CreditoWiCapital(BaseModel):
    negocio_id: str
    nombre_cliente: str = "Desconocido"
    cedula_cliente: str = "Desconocido"
    seccion: str
    estado: str
    sub_estado: str = ""
    fecha: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def estado_completo(self) -> str:
        return f"{self.estado} | {self.sub_estado}" if self.sub_estado else self.estado


# ─────────────────────────────────────────────────────────────────────────────
# RESPUESTAS API
# ─────────────────────────────────────────────────────────────────────────────

class RespuestaBase(BaseModel):
    exito: bool
    mensaje: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RespuestaProspecto(RespuestaBase):
    perfil: Optional[PerfilProspecto] = None
    prospecto: Optional[Prospecto] = None


class RespuestaCampana(RespuestaBase):
    resultado: Optional[ResultadoCampana] = None


class RespuestaAuditoria(RespuestaBase):
    auditoria: Optional[AuditoriaConversacion] = None
