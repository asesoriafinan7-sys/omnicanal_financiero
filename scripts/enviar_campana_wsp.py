"""
scripts/enviar_campana_wsp.py — CLI para campañas masivas de WhatsApp.

Uso:
    # Desde un CSV de contactos:
    python scripts/enviar_campana_wsp.py --csv bases/retoma_enero.csv --plantilla RETOMA_LIBRANZA

    # Desde un archivo JSON de contactos:
    python scripts/enviar_campana_wsp.py --json bases/prospectos.json --plantilla BIENVENIDA

    # Retoma de cliente individual:
    python scripts/enviar_campana_wsp.py --telefono +573001234567 --nombre "Pedro Pérez" \
        --producto LIBRANZA --tasa 1.3

    # Dry-run (sin enviar realmente):
    python scripts/enviar_campana_wsp.py --csv bases/clientes.csv --plantilla RETOMA_LIBRANZA --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.whatsapp_service import WhatsAppCloudAPI, procesar_csv_contactos

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PLANTILLAS_DISPONIBLES = [
    "RETOMA_LIBRANZA",
    "RETOMA_CONSUMO",
    "RETOMA_COMPRA_CARTERA",
    "BIENVENIDA",
    "DOCUMENTOS_PENDIENTES",
    "APROBACION_CREDITO",
    "DESEMBOLSO_LISTO",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Envío masivo de plantillas WhatsApp para campañas comerciales."
    )

    # Fuente de contactos
    grupo_fuente = parser.add_mutually_exclusive_group()
    grupo_fuente.add_argument("--csv", help="Ruta al CSV de contactos.")
    grupo_fuente.add_argument("--json", help="Ruta al JSON de contactos.")
    grupo_fuente.add_argument("--telefono", help="Teléfono individual para envío puntual.")

    # Plantilla
    parser.add_argument(
        "--plantilla", "-p",
        choices=PLANTILLAS_DISPONIBLES,
        default="RETOMA_LIBRANZA",
        help="Nombre de la plantilla a usar.",
    )

    # Campos para envío individual
    parser.add_argument("--nombre", default="Cliente", help="Nombre del contacto (envío individual).")
    parser.add_argument("--producto", default="LIBRANZA", help="Producto (envío individual).")
    parser.add_argument("--tasa", default="1.3", help="Tasa ofrecida (envío individual).")

    # Control
    parser.add_argument("--lote-size", type=int, default=50, help="Envíos por lote antes de pausa.")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Mostrar contactos sin enviar.")

    args = parser.parse_args()

    # ── Cargar contactos ──────────────────────────────────────────────────────
    contactos: List[Dict[str, str]] = []

    if args.telefono:
        # Envío individual
        contactos = [{
            "telefono": args.telefono,
            "nombre": args.nombre,
            "producto": args.producto,
            "tasa": args.tasa,
        }]
    elif args.csv:
        p = Path(args.csv)
        if not p.exists():
            logger.error("CSV no encontrado: %s", args.csv)
            sys.exit(1)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            contactos = procesar_csv_contactos(f.read())
    elif args.json:
        p = Path(args.json)
        if not p.exists():
            logger.error("JSON no encontrado: %s", args.json)
            sys.exit(1)
        with open(p, "r", encoding="utf-8") as f:
            contactos = json.load(f)
    else:
        parser.print_help()
        sys.exit(1)

    if not contactos:
        logger.error("No se encontraron contactos válidos. Verifica el archivo de entrada.")
        sys.exit(1)

    print("\n" + "=" * 55)
    print("  CAMPAÑA WhatsApp — ECOSISTEMA OMNICANAL FINANCIERO")
    print("=" * 55)
    print(f"  Plantilla   : {args.plantilla}")
    print(f"  Contactos   : {len(contactos):,}")
    print(f"  Tamaño lote : {args.lote_size}")
    print(f"  Modo        : {'DRY-RUN (sin envíos reales)' if args.dry_run else '🚀 PRODUCCIÓN'}")
    print("=" * 55 + "\n")

    if args.dry_run:
        logger.info("Dry-run activo. Mostrando primeros 5 contactos:")
        for c in contactos[:5]:
            print(f"  → {c.get('telefono')} | {c.get('nombre')} | {c.get('producto')}")
        if len(contactos) > 5:
            print(f"  ... y {len(contactos) - 5} contactos más.")
        print("\n✅ Dry-run completado. Sin envíos realizados.")
        return

    # Confirmación antes de envío masivo
    if len(contactos) > 10:
        confirmar = input(f"⚠️  ¿Confirmar envío a {len(contactos)} contactos? (s/N): ")
        if confirmar.strip().lower() not in ("s", "si", "sí", "yes", "y"):
            print("Operación cancelada.")
            sys.exit(0)

    # ── Ejecutar envío ────────────────────────────────────────────────────────
    wa = WhatsAppCloudAPI()

    if args.telefono:
        resultado = wa.retomar_cliente(
            telefono=args.telefono,
            nombre_cliente=args.nombre,
            producto=args.producto,
            tasa_oferta=args.tasa,
        )
        print(f"\n✅ Enviado: {resultado}")
    else:
        resultado_masivo = wa.campana_masiva(contactos, args.plantilla, args.lote_size)
        print("\n" + "=" * 55)
        print("  RESUMEN FINAL")
        print("=" * 55)
        print(f"  Total      : {resultado_masivo['total_contactos']:,}")
        print(f"  Enviados   : {resultado_masivo['enviados']:,}")
        print(f"  Fallidos   : {resultado_masivo['fallidos']:,}")
        if resultado_masivo["errores"]:
            print(f"  Primeros errores:")
            for err in resultado_masivo["errores"][:5]:
                print(f"    ⚠️  {err}")
        print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
