"""
routing_service.py — Orquestador de Ruteo de Prospectos.

Conecta el motor de reglas de negocio con los tres canales de outsourcing:
  • WiCapital  → Selenium scraper (ya existente)
  • Expertos   → Email SMTP (AV Villas Libranza/Consumo)
  • Vivienda Total → Email SMTP (Hipotecarios por banco)

Registra la decisión de ruteo en Firestore y en Google Sheets CRM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.business_rules import BusinessRulesEngine, RoutingDecision, get_rules_engine
from app.core.resilience import safe_call_async

logger = logging.getLogger(__name__)


@dataclass
class RuteoResultado:
    exito:      bool
    outsourcing: str
    canal:      str           # "scraper" | "email"
    mensaje:    str
    doc_ref:    Optional[str] = None


class RuteoService:
    """
    Orquesta el ruteo de un prospecto perfilado al outsourcing correcto.
    """

    def __init__(self) -> None:
        self._engine: BusinessRulesEngine = get_rules_engine()

    async def rutar_prospecto(
        self,
        telefono: str,
        nombre: str,
        sector: str,
        producto: str,
        banco_detectado: str,
        perfil_data: Dict[str, Any],
        prioridad: str = "MEDIA",
        ingresos_cop: Optional[float] = None,
        objeciones: list = None,
        resumen_ia: str = "",
        fuente: str = "WHATSAPP_INBOUND",
    ) -> RuteoResultado:
        """
        Punto de entrada principal del ruteo.

        1. Determina outsourcing según product + banco.
        2. Ejecuta la integración correspondiente.
        3. Registra en Firestore + Sheets.
        """
        routing: RoutingDecision = self._engine.get_routing(producto, banco_detectado)
        docs = routing.documentos

        prospecto_data: Dict[str, Any] = {
            "telefono":  telefono,
            "nombre":    nombre,
            "sector":    sector,
            "banco":     banco_detectado,
            "producto":  producto,
            "prioridad": prioridad,
            "ingresos_estimados_cop": ingresos_cop,
            "objeciones": objeciones or [],
            "resumen_ia": resumen_ia,
            "primer_contacto": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "fuente":    fuente,
        }

        logger.info(
            "Ruteo: %s | %s → banco=%s → outsourcing=%s",
            telefono, producto, routing.banco_normalizado, routing.outsourcing,
        )

        # ── Parsear por tipo de integración ──────────────────────────────────
        resultado: RuteoResultado

        if routing.integracion == "email":
            resultado = await self._rutar_por_email(
                routing, prospecto_data, docs
            )
        else:
            resultado = await self._rutar_wicapital(prospecto_data)

        # ── Registrar decisión en Firestore + Sheets ──────────────────────────
        await safe_call_async(
            self._registrar_ruteo,
            telefono, routing, resultado,
            fallback=None,
            service_name="registrar_ruteo",
        )

        return resultado

    # ── Integración: Email ────────────────────────────────────────────────────

    async def _rutar_por_email(
        self,
        routing: RoutingDecision,
        prospecto_data: Dict[str, Any],
        docs: list,
    ) -> RuteoResultado:
        from app.services.email_router import EmailRouter
        email_svc = EmailRouter()

        outsourcing = routing.outsourcing
        exito = False

        try:
            if outsourcing == "EXPERTOS":
                exito = await safe_call_async(
                    email_svc.enviar_perfil_expertos,
                    prospecto_data, docs,
                    fallback=False,
                    critical=True,
                    service_name="EmailRouter.EXPERTOS",
                )
            elif outsourcing == "VIVIENDA_TOTAL":
                exito = await safe_call_async(
                    email_svc.enviar_perfil_vivienda_total,
                    prospecto_data, docs, routing.banco_normalizado,
                    fallback=False,
                    critical=True,
                    service_name="EmailRouter.VIVIENDA_TOTAL",
                )

            canal_msg = routing.email_principal or "email"
            return RuteoResultado(
                exito=bool(exito),
                outsourcing=outsourcing,
                canal="email",
                mensaje=f"Perfil enviado a {outsourcing} ({canal_msg})." if exito
                        else f"Error enviando email a {outsourcing}. Revisa logs.",
            )
        except Exception as exc:
            logger.error("Error en _rutar_por_email: %s", exc, exc_info=True)
            return RuteoResultado(
                exito=False,
                outsourcing=outsourcing,
                canal="email",
                mensaje=str(exc),
            )

    # ── Integración: WiCapital Scraper ────────────────────────────────────────

    async def _rutar_wicapital(self, prospecto_data: Dict[str, Any]) -> RuteoResultado:
        """
        Para WiCapital no ejecutamos el scraper aquí (eso lo hace el monitor
        periódico). Solo marcamos el prospecto para seguimiento y notificamos.
        """
        telefono = prospecto_data.get("telefono", "N/D")
        nombre   = prospecto_data.get("nombre", "Prospecto")

        logger.info(
            "Prospecto %s (%s) asignado a WICAPITAL para seguimiento por scraper.",
            nombre, telefono,
        )
        # Notificar por Telegram al asesor
        try:
            from app.services.crm_sync import TelegramAlerter
            TelegramAlerter().send(
                f"📋 <b>Asignado a WiCapital</b>\n"
                f"👤 <b>Cliente:</b> {nombre}\n"
                f"📱 <b>Tel:</b> {telefono}\n"
                f"🏦 <b>Banco:</b> {prospecto_data.get('banco', 'N/D')}\n"
                f"💳 <b>Producto:</b> {prospecto_data.get('producto', 'N/D')}\n"
                f"⭐ <b>Prioridad:</b> {prospecto_data.get('prioridad', 'MEDIA')}"
            )
        except Exception as e:
            logger.warning("No se pudo notificar Telegram para WiCapital: %s", e)

        return RuteoResultado(
            exito=True,
            outsourcing="WICAPITAL",
            canal="scraper",
            mensaje=f"Prospecto {nombre} registrado para seguimiento en WiCapital.",
        )

    # ── Registro en Firestore + Google Sheets ─────────────────────────────────

    async def _registrar_ruteo(
        self,
        telefono: str,
        routing: RoutingDecision,
        resultado: RuteoResultado,
    ) -> None:
        """Persiste la decisión de ruteo en Firestore y actualiza Sheets."""
        import asyncio
        from google.cloud import firestore
        from app.core.config import get_settings

        settings = get_settings()

        loop = asyncio.get_event_loop()

        # ── Firestore ─────────────────────────────────────────────────────────
        def _write_firestore():
            db = firestore.Client(project=settings.google_cloud_project)
            doc_ref = db.collection("ruteos").document(
                telefono.replace("+", "") + "_" + datetime.now().strftime("%Y%m%d%H%M%S")
            )
            doc_ref.set({
                "telefono":    telefono,
                "outsourcing": routing.outsourcing,
                "integracion": routing.integracion,
                "email":       routing.email_principal,
                "banco":       routing.banco_normalizado,
                "exito":       resultado.exito,
                "mensaje":     resultado.mensaje,
                "timestamp":   firestore.SERVER_TIMESTAMP,
            })

        await loop.run_in_executor(None, _write_firestore)

        # ── Google Sheets — actualizar campo Outsourcing_Asignado ─────────────
        def _update_sheets():
            try:
                from app.services.crm_sync import GoogleSheetsCRM
                crm = GoogleSheetsCRM()
                crm.actualizar_estado_crm(
                    telefono=telefono,
                    nuevo_estado="Perfilado",
                    notas=f"Ruteo → {routing.outsourcing} | {resultado.mensaje}",
                )
            except Exception as exc:
                logger.warning("No se actualizó Sheets en registrar_ruteo: %s", exc)

        await loop.run_in_executor(None, _update_sheets)
