from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from google.cloud import firestore
from app.core.config import get_settings
from app.models.schemas import Prospecto

logger = logging.getLogger(__name__)
_settings = get_settings()

class FirestoreCRM:
    """
    Gestión de prospectos y estados en Firestore (Fuente de Verdad).
    Independiente de Google Sheets para garantizar estabilidad.
    """

    def __init__(self) -> None:
        self._db = firestore.Client(project=_settings.google_cloud_project)
        self._collection = "prospectos"

    def get_prospecto(self, telefono: str) -> Optional[Dict[str, Any]]:
        """Busca un prospecto por su número de teléfono (ID de documento)."""
        try:
            doc_id = telefono.replace("+", "")
            doc = self._db.collection(self._collection).document(doc_id).get()
            return doc.to_dict() if doc.exists else None
        except Exception as exc:
            logger.error("Error leyendo de Firestore: %s", exc)
            return None

    def upsert_prospecto(self, prospecto: Prospecto) -> bool:
        """Guarda o actualiza el prospecto en Firestore."""
        try:
            doc_id = prospecto.telefono.replace("+", "")
            data = prospecto.model_dump()
            self._db.collection(self._collection).document(doc_id).set(data, merge=True)
            logger.info("Firestore: Prospecto %s guardado/actualizado.", prospecto.telefono)
            return True
        except Exception as exc:
            logger.error("Error guardando en Firestore: %s", exc)
            return False

    def actualizar_estado(self, telefono: str, nuevo_estado: str, notas: str = "") -> bool:
        """Actualiza el estado y notas de un prospecto."""
        try:
            doc_id = telefono.replace("+", "")
            update_data = {
                "estado_crm": nuevo_estado,
                "fecha_ultimo_contacto": datetime.utcnow()
            }
            if notas:
                update_data["notas"] = notas
                
            self._db.collection(self._collection).document(doc_id).update(update_data)
            return True
        except Exception as exc:
            logger.error("Error actualizando estado en Firestore: %s", exc)
            return False
