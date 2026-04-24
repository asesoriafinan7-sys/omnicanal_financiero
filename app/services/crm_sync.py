"""
crm_sync.py — Sincronización bidireccional con Google Sheets y notificaciones Telegram.

Módulos:
  • GoogleSheetsCRM  → CRUD de prospectos y campañas en Sheets.
  • WiCapitalMonitor → Scraper de CRM WiCapital + diff Firestore + alertas Telegram.
  • TelegramAlerter  → Envío de mensajes y alertas formateadas.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import gspread
import requests
import google.auth
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.core.config import get_settings
from app.models.schemas import (
    CreditoWiCapital,
    EstadoWiCapital,
    Prospecto,
    PrioridadProspecto,
    ProductoFinanciero,
    SectorEconomico,
)

logger = logging.getLogger(__name__)
_settings = get_settings()

# ─── Scopes requeridos para Google Sheets + Drive ─────────────────────────────
GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─── Secciones a monitorear en WiCapital ────────────────────────────────────
WICAPITAL_SECCIONES = [s.value for s in EstadoWiCapital]


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTER
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramAlerter:
    """Envía mensajes formateados HTML al chat de Telegram configurado."""

    API_URL: str = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._token = _settings.telegram_bot_token
        self._chat_id = _settings.telegram_chat_id

        if not self._token or not self._chat_id:
            logger.warning("TelegramAlerter: credenciales no configuradas — notificaciones desactivadas.")

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Envía un mensaje. Retorna True si fue exitoso."""
        if not self._token or not self._chat_id:
            return False
        url = self.API_URL.format(token=self._token)
        payload = {"chat_id": self._chat_id, "text": message, "parse_mode": parse_mode}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Error enviando mensaje a Telegram: %s", exc)
            return False

    def alerta_nuevo_radicado(self, credito: CreditoWiCapital) -> bool:
        msg = (
            f"🟢 <b>NUEVO RADICADO ({credito.negocio_id})</b>\n"
            f"👤 <b>Cliente:</b> {credito.nombre_cliente}\n"
            f"🆔 <b>Cédula:</b> {credito.cedula_cliente}\n"
            f"📁 <b>Sección:</b> {credito.seccion}\n"
            f"📊 <b>Estado:</b> {credito.estado_completo}\n"
            f"📅 <b>Fecha:</b> {credito.fecha}"
        )
        return self.send(msg)

    def alerta_cambio_estado(
        self,
        credito: CreditoWiCapital,
        seccion_anterior: str,
        estado_anterior: str,
    ) -> bool:
        msg = (
            f"🔄 <b>CAMBIO DE ESTADO ({credito.negocio_id})</b>\n"
            f"👤 <b>Cliente:</b> {credito.nombre_cliente}\n"
            f"🆔 <b>Cédula:</b> {credito.cedula_cliente}\n"
            f"📍 <b>Antes:</b> {seccion_anterior} → {estado_anterior}\n"
            f"🚀 <b>Ahora:</b> {credito.seccion} → {credito.estado_completo}\n"
            f"📅 <b>Fecha:</b> {credito.fecha}"
        )
        return self.send(msg)

    def alerta_prospecto_alta_prioridad(self, prospecto: Prospecto, resumen: str) -> bool:
        msg = (
            f"🔥 <b>PROSPECTO ALTA PRIORIDAD</b>\n"
            f"📱 <b>Tel:</b> {prospecto.telefono}\n"
            f"👤 <b>Nombre:</b> {prospecto.nombre}\n"
            f"🏢 <b>Sector:</b> {prospecto.sector_economico.value}\n"
            f"💰 <b>Producto:</b> {prospecto.producto_interes.value}\n"
            f"📝 <b>Análisis:</b> {resumen}"
        )
        return self.send(msg)

    def alerta_error_critico(self, modulo: str, detalle: str) -> bool:
        msg = (
            f"🚨 <b>ERROR CRÍTICO — {modulo}</b>\n"
            f"⚠️ {detalle}\n"
            f"🕐 {datetime.utcnow().isoformat()}"
        )
        return self.send(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS CRM
# ═══════════════════════════════════════════════════════════════════════════════

class GoogleSheetsCRM:
    """
    Sincronización bidireccional con Google Sheets como CRM ágil.
    Soporta múltiples pestañas: Prospectos, Campañas, Conversiones.
    """

    def __init__(self) -> None:
        self._spreadsheet_id = _settings.gsheets_spreadsheet_id
        self._client = self._build_client()
        self._spreadsheet: Optional[gspread.Spreadsheet] = None

    def _build_client(self) -> gspread.Client:
        """
        Estrategia de autenticación dual (Prioridad descendente):
          1. Si GSHEETS_CREDENTIALS_JSON tiene contenido → Service Account JSON (llave explica).
          2. Si está vacío → Application Default Credentials (ADC):
             - Cloud Run: usa la cuenta de servicio del entorno automáticamente.
             - Local: usa 'gcloud auth application-default login'.
        """
        creds_json = _settings.gsheets_credentials_json
        
        if creds_json and creds_json.strip():
            # ── RUTA 1: Llave JSON explícita (archivo o JSON en string) ──
            try:
                creds_dict = json.loads(creds_json)
            except json.JSONDecodeError:
                # Es posible que venga como ruta de archivo
                with open(creds_json, "r", encoding="utf-8") as f:
                    creds_dict = json.load(f)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=GSHEETS_SCOPES)
            logger.info("GoogleSheets: autenticado vía Service Account JSON explícito.")
        else:
            # ── RUTA 2: Application Default Credentials (ADC) ──
            # Cloud Run usa la identidad del entorno. Local requiere:
            #   gcloud auth application-default login
            credentials, project = google.auth.default(scopes=GSHEETS_SCOPES)
            logger.info(
                "GoogleSheets: autenticado vía ADC (identidad del entorno GCP). Proyecto: %s",
                project or "desconocido"
            )
        
        return gspread.authorize(credentials)

    def _get_spreadsheet(self) -> gspread.Spreadsheet:
        if self._spreadsheet is None:
            self._spreadsheet = self._client.open_by_key(self._spreadsheet_id)
        return self._spreadsheet

    def _get_or_create_worksheet(self, tab_name: str, headers: List[str]) -> gspread.Worksheet:
        ss = self._get_spreadsheet()
        try:
            ws = ss.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(headers))
            ws.append_row(headers, value_input_option="USER_ENTERED")
            logger.info("Hoja '%s' creada con encabezados.", tab_name)
        return ws

    # ── PROSPECTOS ──────────────────────────────────────────────────────────

    def upsert_prospecto(self, prospecto: Prospecto) -> bool:
        """
        Inserta o actualiza un prospecto por número de teléfono.
        Si ya existe, actualiza estado y último contacto.
        """
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_prospectos_tab, Prospecto.sheets_headers()
            )
            # Buscar por teléfono (columna A)
            cell = ws.find(prospecto.telefono, in_column=1)
            row_data = prospecto.to_sheets_row()

            if cell:
                # Actualizar fila existente
                ws.update(f"A{cell.row}:N{cell.row}", [row_data])
                logger.info("Prospecto actualizado: %s", prospecto.telefono)
            else:
                # Insertar nueva fila
                ws.append_row(row_data, value_input_option="USER_ENTERED")
                logger.info("Nuevo prospecto insertado: %s", prospecto.telefono)
            return True
        except Exception as exc:
            logger.error("Error en upsert_prospecto: %s", exc, exc_info=True)
            return False

    def get_prospectos_por_prioridad(
        self, prioridad: PrioridadProspecto
    ) -> List[Dict[str, Any]]:
        """Retorna todos los prospectos filtrados por prioridad."""
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_prospectos_tab, Prospecto.sheets_headers()
            )
            records = ws.get_all_records()
            return [r for r in records if r.get("Prioridad") == prioridad.value]
        except Exception as exc:
            logger.error("Error obteniendo prospectos: %s", exc)
            return []

    def get_prospectos_por_sector(self, sector: SectorEconomico) -> List[Dict[str, Any]]:
        """Retorna prospectos segmentados por sector económico."""
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_prospectos_tab, Prospecto.sheets_headers()
            )
            records = ws.get_all_records()
            return [r for r in records if r.get("Sector") == sector.value]
        except Exception as exc:
            logger.error("Error filtrando por sector: %s", exc)
            return []

    def get_all_prospectos(self) -> List[Dict[str, Any]]:
        """
        Retorna TODOS los prospectos con degradación elegante.
        """
        records = self._safe_get_records(_settings.gsheets_prospectos_tab)
        # Ordenar por Fecha_Creacion desc (más recientes primero)
        records.sort(
            key=lambda r: str(r.get("Fecha_Creacion", "")),
            reverse=True,
        )
        return records

    def get_all_campanas(self) -> List[Dict[str, Any]]:
        """Retorna todas las campañas con degradación elegante."""
        return self._safe_get_records(_settings.gsheets_campanas_tab)

    def get_all_wicapital_data(self) -> List[Dict[str, Any]]:
        """Retorna datos de WiCapital con degradación elegante."""
        return self._safe_get_records(_settings.gsheets_wicapital_tab)

    def _safe_get_records(self, tab_name: str) -> List[Dict[str, Any]]:
        """Helper resiliente para obtener registros de una pestaña."""
        try:
            ss = self._get_spreadsheet()
            # Intento de lectura pura sin auto-creación para evitar mutaciones en GET
            ws = ss.worksheet(tab_name)
            records = ws.get_all_records(default_blank="")
            return records
        except gspread.WorksheetNotFound:
            logger.warning(f"Pestaña '{tab_name}' no existe en Google Sheets. Devolviendo [].")
            return []
        except Exception as exc:
            logger.error(f"Error inesperado leyendo pestaña '{tab_name}': {exc}")
            return []

    def get_prospecto_by_telefono(self, telefono: str) -> Optional[Dict[str, Any]]:
        """
        Busca un prospecto por número de teléfono (exacto o normalizado).
        Retorna el primer match o None.
        """
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_prospectos_tab, Prospecto.sheets_headers()
            )
            records = ws.get_all_records(default_blank="")
            # Normalizar el teléfono de búsqueda
            tel_norm = telefono.replace("+", "").replace(" ", "").replace("-", "")
            for r in records:
                cel = str(r.get("Telefono", "")).replace("+", "").replace(" ", "").replace("-", "")
                if cel == tel_norm:
                    return r
            return None
        except Exception as exc:
            logger.error("Error en get_prospecto_by_telefono: %s", exc)
            return None

    def actualizar_estado_crm(
        self, telefono: str, nuevo_estado: str, notas: str = ""
    ) -> bool:
        """Actualiza el estado CRM de un prospecto existente."""
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_prospectos_tab, Prospecto.sheets_headers()
            )
            cell = ws.find(telefono, in_column=1)
            if not cell:
                logger.warning("Prospecto no encontrado para actualizar: %s", telefono)
                return False
            headers = ws.row_values(1)
            estado_col = headers.index("Estado_CRM") + 1
            notas_col = headers.index("Notas") + 1
            ult_contacto_col = headers.index("Ultimo_Contacto") + 1

            ws.update_cell(cell.row, estado_col, nuevo_estado)
            ws.update_cell(cell.row, notas_col, notas)
            ws.update_cell(cell.row, ult_contacto_col, datetime.utcnow().isoformat())
            return True
        except Exception as exc:
            logger.error("Error actualizando estado CRM: %s", exc)
            return False

    # ── BASES DE DATOS / LIMPIEZA ────────────────────────────────────────────

    def importar_base_datos(
        self,
        registros: List[Dict[str, Any]],
        tab_name: str = "Base_Importada",
    ) -> int:
        """
        Importa una base de datos segmentada y normalizada.
        Normaliza teléfonos a +57XXXXXXXXXX.
        Retorna el número de registros insertados.
        """
        if not registros:
            return 0
        headers = list(registros[0].keys())
        ws = self._get_or_create_worksheet(tab_name, headers)

        rows_to_insert: List[List[Any]] = []
        for r in registros:
            telefono_raw = str(r.get("telefono", r.get("celular", r.get("phone", ""))))
            r["telefono"] = _normalizar_telefono(telefono_raw)
            # r["email"] = _inferir_sector_por_email(r.get("email", ""))
            rows_to_insert.append([r.get(h, "") for h in headers])

        ws.append_rows(rows_to_insert, value_input_option="USER_ENTERED")
        logger.info("%d registros importados en tab '%s'.", len(rows_to_insert), tab_name)
        return len(rows_to_insert)

    # ── CAMPAÑAS ─────────────────────────────────────────────────────────────

    def registrar_campana(self, datos_campana: Dict[str, Any]) -> bool:
        """Registra una nueva campaña de Meta Ads en la hoja de Campañas."""
        try:
            ws = self._get_or_create_worksheet(
                _settings.gsheets_campanas_tab,
                ["ID_Campana", "Nombre", "Sector", "Producto", "Presupuesto_COP",
                 "Fecha_Inicio", "Fecha_Fin", "Estado", "Leads_Generados",
                 "Costo_Por_Lead", "Fecha_Registro"],
            )
            datos_campana["Fecha_Registro"] = datetime.utcnow().isoformat()
            ws.append_row(list(datos_campana.values()), value_input_option="USER_ENTERED")
            return True
        except Exception as exc:
            logger.error("Error registrando campaña: %s", exc)
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# WICAPITAL CRM MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class WiCapitalMonitor:
    """
    Monitorea el CRM de WiCapital via Selenium, persiste el estado en
    Firestore y notifica cambios por Telegram en tiempo real.
    """

    def __init__(self) -> None:
        self._telegram = TelegramAlerter()
        self._db: Optional[firestore.Client] = None

    def _get_db(self) -> firestore.Client:
        if self._db is None:
            self._db = firestore.Client(project=_settings.google_cloud_project)
        return self._db

    @staticmethod
    def _configure_webdriver() -> webdriver.Chrome:
        opts = Options()
        opts.add_argument("--headless")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
        service = Service(executable_path="/usr/bin/chromedriver")
        return webdriver.Chrome(service=service, options=opts)

    def _login(self, driver: webdriver.Chrome) -> bool:
        """Realiza el login en el CRM WiCapital. Retorna True si es exitoso."""
        try:
            driver.get(_settings.wicapital_login_url)
            wait = WebDriverWait(driver, 15)

            user_input = wait.until(EC.presence_of_element_located((By.ID, "d1")))
            user_input.clear()
            user_input.send_keys(_settings.wicapital_user)

            pass_input = driver.find_element(By.NAME, "d2")
            pass_input.clear()
            pass_input.send_keys(_settings.wicapital_pass)

            driver.find_element(By.XPATH, "//*[@type='submit']").click()
            time.sleep(4)
            logger.info("Login en WiCapital exitoso.")
            return True
        except Exception as exc:
            logger.error("Error en login WiCapital: %s", exc, exc_info=True)
            return False

    def _scrape_seccion(
        self, driver: webdriver.Chrome, seccion: str
    ) -> Dict[str, CreditoWiCapital]:
        """Extrae todos los créditos de una sección del CRM."""
        creditos: Dict[str, CreditoWiCapital] = {}
        try:
            wait = WebDriverWait(driver, 15)
            wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, seccion))).click()
            table = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//table[contains(@class, 'table-bordered')]")
                )
            )
            time.sleep(3)

            rows = table.find_elements(By.XPATH, ".//tbody/tr")
            logger.info("Sección '%s': %d filas encontradas.", seccion, len(rows))

            for row in rows:
                cols = row.find_elements(By.TAG_NAME, "td")
                if len(cols) >= 14:
                    negocio_id = cols[0].text.strip()
                    if not negocio_id.isdigit():
                        continue

                    cedula = cols[7].text.strip() if len(cols) > 7 else "Desconocido"
                    nombre = cols[8].text.strip() if len(cols) > 8 else "Desconocido"
                    fecha = cols[2].text.strip()
                    estado = cols[13].text.strip()
                    sub_estado = cols[14].text.strip() if len(cols) > 14 else ""

                    creditos[negocio_id] = CreditoWiCapital(
                        negocio_id=negocio_id,
                        nombre_cliente=nombre,
                        cedula_cliente=cedula,
                        seccion=seccion,
                        estado=estado,
                        sub_estado=sub_estado,
                        fecha=fecha,
                    )
        except Exception as exc:
            logger.error("Error extrayendo sección '%s': %s", seccion, exc)
        return creditos

    def scrape_all_sections(self) -> Dict[str, CreditoWiCapital]:
        """Ejecuta el scraping completo de todas las secciones. Retorna dict por ID."""
        driver = self._configure_webdriver()
        all_credits: Dict[str, CreditoWiCapital] = {}
        try:
            if not self._login(driver):
                self._telegram.alerta_error_critico("WiCapitalMonitor", "Login fallido — verificar credenciales.")
                return {}

            for seccion in WICAPITAL_SECCIONES:
                logger.info("Procesando sección: %s", seccion)
                sección_credits = self._scrape_seccion(driver, seccion)
                all_credits.update(sección_credits)
        finally:
            driver.quit()
        return all_credits

    def process_and_notify(self, current_data: Dict[str, CreditoWiCapital]) -> int:
        """
        Compara el estado actual con Firestore, notifica cambios y actualiza BD.
        Retorna el número de cambios detectados.
        """
        if not current_data:
            logger.warning("No se extrajeron datos — abortando notificación.")
            return 0

        db = self._get_db()
        collection = db.collection(_settings.firestore_collection)
        cambios = 0

        for negocio_id, credito in current_data.items():
            doc_ref = collection.document(negocio_id)
            doc = doc_ref.get()

            if doc.exists:
                data_db = doc.to_dict()
                seccion_anterior = data_db.get("Seccion", "")
                estado_anterior = data_db.get("Estado", "")

                if seccion_anterior != credito.seccion or estado_anterior != credito.estado_completo:
                    logger.info("Cambio detectado: ID %s", negocio_id)
                    self._telegram.alerta_cambio_estado(credito, seccion_anterior, estado_anterior)
                    doc_ref.update({
                        "Seccion": credito.seccion,
                        "Estado": credito.estado_completo,
                        "Fecha": credito.fecha,
                        "Nombre": credito.nombre_cliente,
                        "Cedula": credito.cedula_cliente,
                        "updated_at": firestore.SERVER_TIMESTAMP,
                    })
                    cambios += 1
            else:
                logger.info("Nuevo radicado: ID %s", negocio_id)
                self._telegram.alerta_nuevo_radicado(credito)
                doc_ref.set({
                    "Seccion": credito.seccion,
                    "Estado": credito.estado_completo,
                    "Fecha": credito.fecha,
                    "Nombre": credito.nombre_cliente,
                    "Cedula": credito.cedula_cliente,
                    "created_at": firestore.SERVER_TIMESTAMP,
                })
                cambios += 1

        logger.info("Total de cambios procesados: %d", cambios)
        return cambios

    def run_full_cycle(self) -> Dict[str, Any]:
        """Ciclo completo: scrape → diff → notificar → actualizar Firestore."""
        inicio = time.time()
        logger.info("=== Iniciando ciclo WiCapital Monitor ===")

        current_data = self.scrape_all_sections()
        cambios = self.process_and_notify(current_data)

        duracion = time.time() - inicio
        logger.info("=== Ciclo finalizado en %.2f s — Cambios: %d ===", duracion, cambios)
        return {
            "total_creditos_detectados": len(current_data),
            "cambios_notificados": cambios,
            "duracion_segundos": round(duracion, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ORGANIZADOR DE BASES DE DATOS (organizador_interactivo.py refactorizado)
# ═══════════════════════════════════════════════════════════════════════════════

# Mapeo de dominios de correo a sectores económicos
_DOMINIOS_SECTOR: Dict[str, SectorEconomico] = {
    # Salud
    "hospitaluniversitario": SectorEconomico.SALUD,
    "clinica": SectorEconomico.SALUD,
    "hci": SectorEconomico.SALUD,
    "salud": SectorEconomico.SALUD,
    "hospital": SectorEconomico.SALUD,
    "ips": SectorEconomico.SALUD,
    "eps": SectorEconomico.SALUD,
    "medicos": SectorEconomico.SALUD,
    # Educación
    "edu.co": SectorEconomico.EDUCACION,
    "universit": SectorEconomico.EDUCACION,
    "colegio": SectorEconomico.EDUCACION,
    "escuela": SectorEconomico.EDUCACION,
    "inem": SectorEconomico.EDUCACION,
    # Gobierno
    "gov.co": SectorEconomico.GOBIERNO,
    "gobernacion": SectorEconomico.GOBIERNO,
    "alcaldia": SectorEconomico.GOBIERNO,
    "mindefensa": SectorEconomico.FUERZAS_MILITARES,
    "ejercito": SectorEconomico.FUERZAS_MILITARES,
    "policia": SectorEconomico.POLICIA_NACIONAL,
    # Energía / Petróleo
    "ecopetrol": SectorEconomico.SECTOR_PETROLERO,
    "petro": SectorEconomico.SECTOR_PETROLERO,
    "enel": SectorEconomico.SECTOR_ENERGETICO,
    "celsia": SectorEconomico.SECTOR_ENERGETICO,
    "epsa": SectorEconomico.SECTOR_ENERGETICO,
    # Construcción
    "construrama": SectorEconomico.SECTOR_CONSTRUCCION,
    "obra": SectorEconomico.SECTOR_CONSTRUCCION,
    # Tecnología
    "softw": SectorEconomico.SECTOR_TECNOLOGIA,
    "tech": SectorEconomico.SECTOR_TECNOLOGIA,
    "sistemas": SectorEconomico.SECTOR_TECNOLOGIA,
    # Financiero
    "bancolombia": SectorEconomico.SECTOR_FINANCIERO,
    "davivienda": SectorEconomico.SECTOR_FINANCIERO,
    "bogota": SectorEconomico.SECTOR_FINANCIERO,
    "banco": SectorEconomico.SECTOR_FINANCIERO,
    "seguros": SectorEconomico.SECTOR_FINANCIERO,
    "fiduci": SectorEconomico.SECTOR_FINANCIERO,
    # Agropecuario
    "agro": SectorEconomico.SECTOR_AGROPECUARIO,
    "campo": SectorEconomico.SECTOR_AGROPECUARIO,
    "fedegan": SectorEconomico.SECTOR_AGROPECUARIO,
}


def _normalizar_telefono(raw: str) -> str:
    """Estandariza números colombianos a +57XXXXXXXXXX."""
    digitos = re.sub(r"\D", "", str(raw))
    if digitos.startswith("57") and len(digitos) == 12:
        return f"+{digitos}"
    if len(digitos) == 10 and digitos[0] in ("3", "6"):
        return f"+57{digitos}"
    if len(digitos) == 7:  # Fijo local sin indicativo
        return f"+576{digitos}"
    return f"+{digitos}" if digitos else raw


def _inferir_sector_por_email(email: str) -> str:
    """Infiere el sector económico basado en el dominio del correo electrónico."""
    email_lower = email.lower()
    for patron, sector in _DOMINIOS_SECTOR.items():
        if patron in email_lower:
            return sector.value
    return SectorEconomico.DESCONOCIDO.value


def limpiar_y_segmentar_base(
    registros_raw: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Limpia, normaliza y segmenta una base de datos por sector económico.
    Funciona con bases de cualquier sector (salud, educación, gobierno, etc.)
    Retorna dict {sector: [registros_limpios]}.
    """
    por_sector: Dict[str, List[Dict[str, Any]]] = {}
    vistos: set = set()

    for reg in registros_raw:
        # Normalizar teléfono
        telefono_fields = ["telefono", "celular", "phone", "movil", "tel"]
        telefono_raw = ""
        for f in telefono_fields:
            val = str(reg.get(f, "")).strip()
            if val and val not in ("nan", "None", ""):
                telefono_raw = val
                break

        telefono = _normalizar_telefono(telefono_raw)
        if not telefono or telefono in vistos:
            continue  # Deduplicar
        vistos.add(telefono)

        # Email normalizado
        email = str(reg.get("email", reg.get("correo", ""))).strip().lower()

        # Inferir sector
        sector = reg.get("sector", _inferir_sector_por_email(email))

        # Limpiar nombre
        nombre_fields = ["nombre", "name", "nombres", "nombre_completo"]
        nombre = "Desconocido"
        for f in nombre_fields:
            val = str(reg.get(f, "")).strip().title()
            if val and val not in ("Nan", "None", ""):
                nombre = val
                break

        registro_limpio = {
            "telefono": telefono,
            "nombre": nombre,
            "email": email,
            "sector": sector,
            "cedula": str(reg.get("cedula", reg.get("documento", ""))).strip(),
            "cargo": str(reg.get("cargo", reg.get("ocupacion", ""))).strip(),
            "empresa": str(reg.get("empresa", reg.get("entidad", ""))).strip(),
            "ciudad": str(reg.get("ciudad", "")).strip().title(),
        }

        por_sector.setdefault(sector, []).append(registro_limpio)

    logger.info(
        "Base limpiada: %d registros únicos en %d sectores.",
        len(vistos), len(por_sector),
    )
    return por_sector
