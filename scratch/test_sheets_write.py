import os
import json
import logging
from datetime import datetime
from app.services.crm_sync import GoogleSheetsCRM
from app.models.schemas import Prospecto, PrioridadProspecto, ProductoFinanciero, SectorEconomico

# Configurar logging básico
logging.basicConfig(level=logging.INFO)

def test_write():
    # Asegurarse de que el ID de la hoja sea el correcto
    os.environ["GSHEETS_SPREADSHEET_ID"] = "10633gX6FS_wwUxN8M97mabHUiNl8-A8-7VqwfeCztko"
    
    crm = GoogleSheetsCRM()
    
    test_lead = Prospecto(
        telefono="+573111111111",
        nombre="TEST_ANTIGRAVITY_PROD",
        sector_economico=SectorEconomico.DESCONOCIDO,
        producto_interes=ProductoFinanciero.LIBRANZA,
        prioridad=PrioridadProspecto.MEDIA,
        estado_crm="NUEVO",
        notas=f"Prueba de escritura desde Antigravity - {datetime.now().isoformat()}"
    )
    
    print(f"Intentando escribir en Spreadsheet ID: {os.environ['GSHEETS_SPREADSHEET_ID']}")
    success = crm.upsert_prospecto(test_lead)
    
    if success:
        print("✅ ÉXITO: El sistema pudo escribir en Google Sheets.")
    else:
        print("❌ FALLO: No se pudo escribir. Verifica los permisos de la Service Account.")

if __name__ == "__main__":
    test_write()
