"""
scripts/cargar_base_datos.py — Utilidad CLI para importar bases de datos al CRM.

Uso:
    python scripts/cargar_base_datos.py --archivo bases/clientes_salud.xlsx
    python scripts/cargar_base_datos.py --archivo bases/clientes_empresas.csv --sector EMPRESAS_PRIVADAS
    python scripts/cargar_base_datos.py --archivo bases/clientes_mixtos.xlsx --tab "Importacion_Q2_2025"

Funcionalidades:
  • Lee archivos Excel (.xlsx) y CSV.
  • Limpia, normaliza teléfonos a +57 y deduplica registros.
  • Segmenta automáticamente por sector vía dominio de correo o columna 'sector'.
  • Sube los registros a Google Sheets CRM por pestañas de sector.
  • Genera reporte de resumen en consola.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Agregar root al path para importaciones del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.core.config import get_settings
from app.services.crm_sync import GoogleSheetsCRM, limpiar_y_segmentar_base

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def leer_archivo(ruta: str) -> List[Dict[str, Any]]:
    """Lee un archivo Excel o CSV y retorna lista de diccionarios."""
    p = Path(ruta)
    if not p.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {ruta}")

    ext = p.suffix.lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(ruta, dtype=str)
    elif ext == ".csv":
        # Intentar con diferentes separadores
        for sep in [",", ";", "\t", "|"]:
            try:
                df = pd.read_csv(ruta, sep=sep, dtype=str, encoding="utf-8")
                if len(df.columns) > 1:
                    break
            except Exception:
                continue
        else:
            df = pd.read_csv(ruta, dtype=str, encoding="latin-1")
    else:
        raise ValueError(f"Formato no soportado: {ext}. Use .xlsx, .xls o .csv")

    # Limpiar nombres de columnas
    df.columns = [c.lower().strip().replace(" ", "_").replace("á", "a")
                  .replace("é", "e").replace("í", "i").replace("ó", "o")
                  .replace("ú", "u") for c in df.columns]

    # Reemplazar NaN con cadena vacía
    df = df.fillna("")

    registros = df.to_dict(orient="records")
    logger.info("Archivo leído: %d registros, columnas: %s", len(registros), list(df.columns))
    return registros


def generar_reporte(por_sector: Dict[str, List[Dict[str, Any]]], tab_base: str) -> None:
    """Imprime reporte de resumen del proceso de importación."""
    total = sum(len(v) for v in por_sector.values())
    print("\n" + "=" * 60)
    print("  REPORTE DE IMPORTACIÓN — ECOSISTEMA OMNICANAL FINANCIERO")
    print("=" * 60)
    print(f"  Total registros únicos procesados : {total:,}")
    print(f"  Sectores detectados               : {len(por_sector)}")
    print(f"  Tab destino base                  : {tab_base}")
    print("-" * 60)
    print(f"  {'SECTOR':<35} {'REGISTROS':>10}")
    print("-" * 60)
    for sector, registros in sorted(por_sector.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"  {sector:<35} {len(registros):>10,}")
    print("=" * 60 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importador de bases de datos al CRM Google Sheets."
    )
    parser.add_argument(
        "--archivo", "-a",
        required=True,
        help="Ruta al archivo .xlsx o .csv a importar.",
    )
    parser.add_argument(
        "--sector", "-s",
        default=None,
        help="Forzar un sector para TODOS los registros. Omitir para detección automática.",
    )
    parser.add_argument(
        "--tab", "-t",
        default="Base_Importada",
        help="Nombre base de la pestaña en Google Sheets (se añade _SECTOR automáticamente).",
    )
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        help="Solo procesar y mostrar reporte sin subir a Sheets.",
    )
    parser.add_argument(
        "--max-registros", "-m",
        type=int,
        default=None,
        help="Límite de registros a procesar (útil para testing).",
    )
    args = parser.parse_args()

    logger.info("Iniciando importación desde: %s", args.archivo)

    # 1. Leer archivo
    registros = leer_archivo(args.archivo)

    # Aplicar límite si se especificó
    if args.max_registros:
        registros = registros[: args.max_registros]
        logger.info("Aplicado límite de %d registros.", args.max_registros)

    # 2. Forzar sector si se especificó
    if args.sector:
        for r in registros:
            r["sector"] = args.sector.upper()
        logger.info("Sector forzado para todos los registros: %s", args.sector)

    # 3. Limpiar y segmentar
    por_sector = limpiar_y_segmentar_base(registros)

    # 4. Mostrar reporte
    generar_reporte(por_sector, args.tab)

    if args.dry_run:
        logger.info("Modo dry-run activo — NO se subió nada a Google Sheets.")
        return

    # 5. Subir a Google Sheets
    logger.info("Subiendo registros a Google Sheets CRM...")
    settings = get_settings()
    if not settings.gsheets_spreadsheet_id:
        logger.error("GSHEETS_SPREADSHEET_ID no configurado. Configura el .env y reintenta.")
        sys.exit(1)

    crm = GoogleSheetsCRM()
    total_insertados = 0

    for sector, recs in por_sector.items():
        tab_nombre = f"{args.tab}_{sector}"
        n = crm.importar_base_datos(recs, tab_name=tab_nombre)
        total_insertados += n
        logger.info("  ✅ %s: %d registros en pestaña '%s'", sector, n, tab_nombre)

    logger.info("🎯 Importación completada: %d registros en Google Sheets.", total_insertados)


if __name__ == "__main__":
    main()
