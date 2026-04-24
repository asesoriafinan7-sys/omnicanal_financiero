import asyncio
import logging
from datetime import datetime
from typing import Dict, Any

from app.services.crm_sync import WiCapitalMonitor, GoogleSheetsCRM, TelegramAlerter
from app.models.schemas import CreditoWiCapital
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("SyncWiCapitalSheets")

_settings = get_settings()

def limpiar_nombre(nombre: str) -> str:
    """Capitaliza los nombres (ej. juan perez -> Juan Perez)."""
    return " ".join([word.capitalize() for word in nombre.lower().split()])

def limpiar_fecha(fecha: str) -> str:
    """Intenta estandarizar fechas, aunque en scraper usualmente ya vienen limpias."""
    return fecha.strip()

def sincronizar() -> None:
    logger.info("=== Iniciando Extracción de WiCapital CRM ===")
    
    # 1. Scrapear WiCapital
    monitor = WiCapitalMonitor()
    current_data = monitor.scrape_all_sections()
    
    if not current_data:
        logger.error("No se pudieron extraer datos de WiCapital. Abortando sincronización.")
        return
        
    logger.info(f"Extracción completada. {len(current_data)} radicados encontrados.")
    
    # 2. Conectar a Google Sheets
    sheets_crm = GoogleSheetsCRM()
    import os
    tab_name = os.getenv("GSHEETS_WICAPITAL_TAB", "WiCapital")
    
    headers = [
        "ID", 
        "Nombre", 
        "Cédula", 
        "Sección", 
        "Estado", 
        "Sub Estado", 
        "Fecha", 
        "Última actualización"
    ]
    
    ws = sheets_crm._get_or_create_worksheet(tab_name, headers)
    
    # Cargar todos los registros actuales para deduplicación rápida usando gspread get_all_records o manual
    # Para ser eficientes con las peticiones a la API, bajaremos toda la tabla 
    todas_filas = ws.get_all_values()
    
    # Mapear IDs a número de fila (las filas en Sheet son 1-indexed)
    # Si la hoja estaba nueva, todas_filas solo tiene la fila 1 (cabeceras).
    # ID está en el index 0.
    mapa_ids = {}
    if len(todas_filas) > 1:
        # Fila 1 = headers. Fila 2 = index 1
        for i, fila in enumerate(todas_filas[1:], start=2):
            if fila and len(fila) > 0:
                mapa_ids[fila[0]] = i
                
    nuevos = 0
    actualizados = 0
    
    ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Procesar en lotes si es necesario, pero iterar 1 por 1 es más fácil para upsert o hacer append_rows
    filas_a_insertar = []
    celdas_a_actualizar = []
    
    logger.info("=== Iniciando Deduplicación y Limpieza ===")
    for cred_id, cred in current_data.items():
        nombre_limpio = limpiar_nombre(cred.nombre_cliente)
        fecha_limpia = limpiar_fecha(cred.fecha)
        
        row_data = [
            cred.negocio_id,
            nombre_limpio,
            cred.cedula_cliente,
            cred.seccion,
            cred.estado_completo,
            cred.sub_estado,
            fecha_limpia,
            ahora_str
        ]
        
        if cred_id in mapa_ids:
            # Actualizar
            num_fila = mapa_ids[cred_id]
            # No sobreescribimos todo si no queremos gastar cuota, pero por simplicidad:
            ws.update(f"A{num_fila}:H{num_fila}", [row_data])
            actualizados += 1
        else:
            # Insertar
            filas_a_insertar.append(row_data)
            nuevos += 1
            
    # Insertar todos los nuevos al mismo tiempo
    if filas_a_insertar:
        ws.append_rows(filas_a_insertar, value_input_option="USER_ENTERED")
        
    logger.info(f"Sincronización finalizada. Nuevos: {nuevos}. Actualizados: {actualizados}.")
    
    # 3. Enviar Reporte a Telegram
    telegram = TelegramAlerter()
    mensaje_telegram = (
        f"✅ <b>Sincronización Exitosa</b>\n"
        f"Se añadieron {nuevos} prospectos nuevos y "
        f"se actualizaron {actualizados} existentes."
    )
    telegram.send(mensaje_telegram)
    logger.info("Notificación enviada por Telegram.")

if __name__ == "__main__":
    sincronizar()
