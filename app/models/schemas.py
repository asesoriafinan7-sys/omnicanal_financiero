from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import re

# --- ENUMS ---
class ProductoFinanciero(str, Enum):
    LIBRANZA = "LIBRANZA"; CONSUMO = "CONSUMO"; COMPRA_CARTERA = "COMPRA_CARTERA"
    MICROFINANZAS = "MICROFINANZAS"; DESCONOCIDO = "DESCONOCIDO"

class SectorEconomico(str, Enum):
    SALUD = "SALUD"; EDUCACION = "EDUCACION"; FUERZAS_MILITARES = "FUERZAS_MILITARES"
    POLICIA_NACIONAL = "POLICIA_NACIONAL"; GOBIERNO = "GOBIERNO"; EMPRESAS_PRIVADAS = "EMPRESAS_PRIVADAS"
    PENSIONADOS = "PENSIONADOS"; INDEPENDIENTES = "INDEPENDIENTES"; SECTOR_ENERGETICO = "SECTOR_ENERGETICO"
    SECTOR_PETROLERO = "SECTOR_PETROLERO"; SECTOR_MINERO = "SECTOR_MINERO"; SECTOR_FINANCIERO = "SECTOR_FINANCIERO"
    SECTOR_TECNOLOGIA = "SECTOR_TECNOLOGIA"; SECTOR_CONSTRUCCION = "SECTOR_CONSTRUCCION"
    SECTOR_AGROPECUARIO = "SECTOR_AGROPECUARIO"; DESCONOCIDO = "DESCONOCIDO"

class PrioridadProspecto(str, Enum):
    ALTA = "ALTA"; MEDIA = "MEDIA"; BAJA = "BAJA"; DESCALIFICADO = "DESCALIFICADO"

class ObjecionDetectada(str, Enum):
    TASA = "OBJECION_TASA"; CAPACIDAD_ENDEUDAMIENTO = "OBJECION_CAPACIDAD_ENDEUDAMIENTO"
    DOCUMENTACION = "OBJECION_DOCUMENTACION"; TIEMPO = "OBJECION_TIEMPO"
    COMPETENCIA = "OBJECION_COMPETENCIA"; NINGUNA = "SIN_OBJECION"

class EstadoWiCapital(str, Enum):
    FILTROS = "Gestión Filtros"; RADICADOS = "Gestión Radicados"; APROBADOS = "Gestión Aprobados"; DESEMBOLSO = "Gestión Desembolso"

# --- WHATSAPP ---
class WAProfile(BaseModel): name: Optional[str] = None
class WAContact(BaseModel): profile: Optional[WAProfile] = None; wa_id: Optional[str] = None
class WATextMessage(BaseModel): body: str
class WAInteractiveButtonReply(BaseModel): id: str; title: Optional[str] = None
class WAInteractiveMessage(BaseModel): type: str; button_reply: Optional[WAInteractiveButtonReply] = None
class WAMessage(BaseModel):
    from_: str = Field(alias="from"); id: str; timestamp: str; type: str
    text: Optional[WATextMessage] = None; interactive: Optional[WAInteractiveMessage] = None
    model_config = {"populate_by_name": True}

class WAValue(BaseModel):
    messaging_product: str; metadata: Optional[Dict[str, Any]] = None
    contacts: Optional[List[WAContact]] = None; messages: Optional[List[WAMessage]] = None; statuses: Optional[List[Dict[str, Any]]] = None

class WAChange(BaseModel): value: WAValue; field: str
class WAEntry(BaseModel): id: str; changes: List[WAChange]
class WhatsAppWebhook(BaseModel): object: str; entry: List[WAEntry]

# --- PROSPECTO ---
class PerfilProspecto(BaseModel):
    califica: bool; producto_detectado: ProductoFinanciero = ProductoFinanciero.DESCONOCIDO
    sector_economico: SectorEconomico = SectorEconomico.DESCONOCIDO; prioridad: PrioridadProspecto = PrioridadProspecto.BAJA
    ingresos_estimados_cop: Optional[float] = None; tiene_deuda_activa: Optional[bool] = None
    objeciones: List[ObjecionDetectada] = Field(default_factory=list); resumen_analisis: str = ""
    confianza_score: float = Field(default=0.0); respuesta_sugerida: str = ""

class Prospecto(BaseModel):
    telefono: str; nombre: str = "Desconocido"; email: Optional[str] = None
    sector_economico: SectorEconomico = SectorEconomico.DESCONOCIDO; producto_interes: ProductoFinanciero = ProductoFinanciero.DESCONOCIDO
    prioridad: PrioridadProspecto = PrioridadProspecto.BAJA; estado_crm: str = "NUEVO"
    ingresos_estimados_cop: Optional[float] = None; objeciones: List[str] = Field(default_factory=list)
    notas: str = ""; fuente: str = "WHATSAPP_INBOUND"; fecha_creacion: datetime = Field(default_factory=datetime.utcnow)
    fecha_ultimo_contacto: datetime = Field(default_factory=datetime.utcnow); campana_origen: Optional[str] = None

    @field_validator("telefono")
    @classmethod
    def normalizar_telefono(cls, v: str) -> str:
        digitos = re.sub(r"\D", "", v)
        if digitos.startswith("57") and len(digitos) == 12: return f"+{digitos}"
        if len(digitos) == 10: return f"+57{digitos}"
        return f"+{digitos}" if not v.startswith("+") else v

    def to_sheets_row(self): return [self.telefono, self.nombre, self.email or "", self.sector_economico.value, self.producto_interes.value, self.prioridad.value, self.estado_crm, self.ingresos_estimados_cop or "", "; ".join(self.objeciones), self.notas, self.fuente, self.fecha_creacion.isoformat(), self.fecha_ultimo_contacto.isoformat(), self.campana_origen or ""]

    @classmethod
    def sheets_headers(cls): return ["Telefono", "Nombre", "Email", "Sector", "Producto", "Prioridad", "Estado_CRM", "Ingresos_COP", "Objeciones", "Notas", "Fuente", "Fecha_Creacion", "Ultimo_Contacto", "Campana_Origen"]

# --- AUDITORIA & CAMPAÑAS ---
class AuditoriaConversacion(BaseModel):
    telefono: str; nombre_contacto: str = "Desconocido"; total_mensajes: int = 0
    primer_mensaje: Optional[datetime] = None; ultimo_mensaje: Optional[datetime] = None
    objeciones_detectadas: List[ObjecionDetectada] = Field(default_factory=list); sentimiento_general: str = "NEUTRO"
    conversion_lograda: bool = False; motivo_no_conversion: str = ""; resumen_ejecutivo: str = ""; mensajes_clave: List[str] = Field(default_factory=list)

class ParametrosCampana(BaseModel):
    nombre: str; objetivo: str = "LEAD_GENERATION"; sector_objetivo: SectorEconomico; producto_financiero: ProductoFinanciero
    presupuesto_diario_cop: float = Field(ge=10_000); fecha_inicio: str; fecha_fin: Optional[str] = None; gancho_comercial: str = ""
    ubicaciones_geo: List[str] = Field(default_factory=lambda: ["CO"]); rango_edad_min: int = 25; rango_edad_max: int = 60
    genero: str = "ALL"; intereses_ids: List[str] = Field(default_factory=list); imagen_creativo_url: Optional[str] = None; texto_anuncio: str = ""; cta: str = "LEARN_MORE"

class ResultadoCampana(BaseModel): exito: bool; campaign_id: Optional[str] = None; adset_id: Optional[str] = None; ad_id: Optional[str] = None; mensaje: str = ""; datos_raw: Optional[Dict[str, Any]] = None

# --- RESPUESTAS ---
class RespuestaBase(BaseModel): exito: bool; mensaje: str; timestamp: datetime = Field(default_factory=datetime.utcnow)
class RespuestaProspecto(RespuestaBase): perfil: Optional[PerfilProspecto] = None; prospecto: Optional[Prospecto] = None
class RespuestaCampana(RespuestaBase): resultado: Optional[ResultadoCampana] = None
class RespuestaAuditoria(RespuestaBase): auditoria: Optional[AuditoriaConversacion] = None

class CreditoWiCapital(BaseModel):
    negocio_id: str; nombre_cliente: str = "Desconocido"; cedula_cliente: str = "Desconocido"
    seccion: str; estado: str; sub_estado: str = ""; fecha: str = ""
    @property
    def estado_completo(self) -> str: return f"{self.estado} | {self.sub_estado}" if self.sub_estado else self.estado

