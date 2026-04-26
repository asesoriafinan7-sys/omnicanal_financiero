import asyncio
import os
import logging
from datetime import datetime, timedelta
from app.core.office_hours import enqueue_message, process_queue
from app.core.config import get_settings

# Configurar logging para ver el delay de 4s
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TestReactivacion")

async def test_full_flow():
    os.environ["APP_ENV"] = "development"
    settings = get_settings()
    
    print("\n--- PASO 1: SIMULANDO LEAD DE MADRUGADA ---")
    # Inyectamos el mensaje directamente en la cola
    telefono_test = "+573111111111"
    nombre_test = "Mauricio Lead Test"
    texto_test = "Hola, vi tu publicidad de créditos de libranza anoche. ¿Qué requisitos piden para pensionados?"
    
    # Usamos enqueue_message que ya guarda en Firestore
    doc_id = enqueue_message(telefono_test, texto_test, nombre_test)
    print(f"✅ Mensaje encolado en Firestore con ID: {doc_id}")

    print("\n--- PASO 2: EJECUTANDO MOTOR DE REACTIVACIÓN (08:00 AM) ---")
    print("Nota: El sistema debería esperar 4 segundos entre mensajes y aplicar contexto temporal.")
    
    # Ejecutamos el vaciado de cola
    # max_items=1 para que sea rápido en la prueba
    procesados = await process_queue(max_items=1)
    
    if procesados > 0:
        print(f"\n✅ ÉXITO: Se procesaron {procesados} mensajes de la cola.")
        print("Revisa los logs arriba para confirmar el 'await asyncio.sleep(4.0)' y la respuesta de la IA.")
    else:
        print("\n❌ FALLO: No se procesó ningún mensaje. Asegúrate de estar en Horario Laboral o de que el mock de horario permita procesar.")

if __name__ == "__main__":
    asyncio.run(test_full_flow())
