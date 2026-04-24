"""
scripts/auditar_chats.py — Auditoría masiva de chats exportados de WhatsApp.

Uso:
    # Auditar todos los .txt en una carpeta:
    python scripts/auditar_chats.py --carpeta chats/

    # Auditar un chat específico:
    python scripts/auditar_chats.py --archivo chats/pedro_perez_whatsapp.txt

    # Solo análisis local (sin Vertex AI, más rápido):
    python scripts/auditar_chats.py --carpeta chats/ --solo-local

    # Exportar resultados a Excel:
    python scripts/auditar_chats.py --carpeta chats/ --exportar resultados_auditoria.xlsx
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.whatsapp_service import analizar_chat_exportado_local

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Patrón para extraer nombre del archivo: "Pedro Pérez - Chat de WhatsApp.txt"
_PATRON_NOMBRE = re.compile(r"^(.+?)[\s\-–]+(?:chat|Chat|whatsapp|WhatsApp)", re.IGNORECASE)


def _extraer_nombre_del_archivo(nombre_archivo: str) -> str:
    m = _PATRON_NOMBRE.match(Path(nombre_archivo).stem)
    return m.group(1).strip() if m else Path(nombre_archivo).stem


async def auditar_un_chat(
    archivo: Path,
    con_ia: bool = True,
) -> Dict[str, Any]:
    nombre_contacto = _extraer_nombre_del_archivo(archivo.name)

    with open(archivo, "r", encoding="utf-8", errors="replace") as f:
        contenido = f.read()

    analisis_local = analizar_chat_exportado_local(contenido)

    resultado: Dict[str, Any] = {
        "archivo": archivo.name,
        "nombre_contacto": nombre_contacto,
        "telefono": "Desconocido",
        "analisis_local": analisis_local,
        "auditoria_ia": None,
    }

    if con_ia:
        try:
            from app.services.ai_engine import auditar_chat_exportado_llama

            auditoria = await auditar_chat_exportado_llama(
                chat_texto=contenido,
                nombre_contacto=nombre_contacto,
            )
            resultado["auditoria_ia"] = auditoria.model_dump()
        except Exception as exc:
            logger.error("Error IA en '%s': %s", archivo.name, exc)
            resultado["auditoria_ia"] = {"error": str(exc)}

    return resultado


async def procesar_carpeta(
    carpeta: Path,
    con_ia: bool = True,
    exportar: str | None = None,
) -> None:
    archivos_txt = sorted(carpeta.glob("*.txt"))
    if not archivos_txt:
        logger.warning("No se encontraron archivos .txt en: %s", carpeta)
        return

    logger.info("Procesando %d archivos de chat...", len(archivos_txt))
    resultados: List[Dict[str, Any]] = []

    for i, archivo in enumerate(archivos_txt, 1):
        logger.info("[%d/%d] Auditando: %s", i, len(archivos_txt), archivo.name)
        r = await auditar_un_chat(archivo, con_ia)
        resultados.append(r)

        # Mostrar resumen en consola
        local = r["analisis_local"]
        ia = r.get("auditoria_ia") or {}
        print(f"\n{'─'*55}")
        print(f"  {r['nombre_contacto']}")
        print(f"  Mensajes    : {local['total_mensajes']}")
        print(f"  Objeciones  : {', '.join(local['objeciones_detectadas_local']) or 'Ninguna'}")
        if ia:
            print(f"  Conversión  : {'✅ SÍ' if ia.get('conversion_lograda') else '❌ NO'}")
            print(f"  Sentimiento : {ia.get('sentimiento_general', 'N/A')}")
            print(f"  Resumen     : {ia.get('resumen_ejecutivo', '')[:120]}...")

    # ── Exportar a Excel si se solicita ──────────────────────────────────────
    if exportar:
        try:
            import pandas as pd

            filas = []
            for r in resultados:
                local = r["analisis_local"]
                ia = r.get("auditoria_ia") or {}
                filas.append({
                    "Archivo": r["archivo"],
                    "Contacto": r["nombre_contacto"],
                    "Total_Mensajes": local["total_mensajes"],
                    "Objeciones_Local": "; ".join(local["objeciones_detectadas_local"]),
                    "Objeciones_IA": "; ".join(ia.get("objeciones_detectadas", [])),
                    "Conversion": ia.get("conversion_lograda", "N/A"),
                    "Sentimiento": ia.get("sentimiento_general", "N/A"),
                    "Motivo_No_Conversion": ia.get("motivo_no_conversion", ""),
                    "Resumen_Ejecutivo": ia.get("resumen_ejecutivo", ""),
                    "Primer_Mensaje": local.get("primer_mensaje_fecha", ""),
                    "Ultimo_Mensaje": local.get("ultimo_mensaje_fecha", ""),
                })

            df = pd.DataFrame(filas)
            df.to_excel(exportar, index=False, sheet_name="Auditoria_Chats")
            print(f"\n✅ Resultados exportados a: {exportar}")
        except ImportError:
            logger.error("pandas/openpyxl no instalados. pip install pandas openpyxl")
        except Exception as exc:
            logger.error("Error exportando: %s", exc)

    # Resumen global
    total = len(resultados)
    if resultados:
        ias = [r.get("auditoria_ia") or {} for r in resultados]
        conversiones = sum(1 for ia in ias if ia.get("conversion_lograda"))
        print(f"\n{'='*55}")
        print(f"  RESUMEN GLOBAL — {total} chats auditados")
        print(f"  Conversiones : {conversiones}/{total} ({conversiones/total*100:.1f}%)")
        print(f"  Sin conversión: {total-conversiones}/{total}")
        print(f"{'='*55}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auditoría de chats exportados de WhatsApp con IA."
    )
    grupo_fuente = parser.add_mutually_exclusive_group(required=True)
    grupo_fuente.add_argument("--carpeta", "-c", help="Carpeta con archivos .txt de chats.")
    grupo_fuente.add_argument("--archivo", "-a", help="Archivo .txt individual de chat.")

    parser.add_argument(
        "--solo-local", "-l",
        action="store_true",
        help="Solo análisis local por keywords, sin llamar a Vertex AI.",
    )
    parser.add_argument(
        "--exportar", "-e",
        default=None,
        help="Ruta del archivo .xlsx donde exportar los resultados.",
    )

    args = parser.parse_args()
    con_ia = not args.solo_local

    if args.carpeta:
        carpeta = Path(args.carpeta)
        if not carpeta.is_dir():
            logger.error("No es un directorio válido: %s", args.carpeta)
            sys.exit(1)
        asyncio.run(procesar_carpeta(carpeta, con_ia, args.exportar))

    elif args.archivo:
        archivo = Path(args.archivo)
        if not archivo.exists():
            logger.error("Archivo no encontrado: %s", args.archivo)
            sys.exit(1)
        resultado = asyncio.run(auditar_un_chat(archivo, con_ia))
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
