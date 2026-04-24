"""
email_router.py — Servicio SMTP para ruteo automático de perfiles a outsourcings.

Configuración: Gmail con SMTP_SSL (puerto 465) o STARTTLS (puerto 587).
Cuenta remitente: asesoriafinan7@gmail.com (requiere Contraseña de Aplicación de Google).

Destinos configurados:
  • Expertos (AV Villas Libranza/Consumo) → 2expertos.bogota@gmail.com
  • Vivienda Total Banco Bogotá           → mesa.bcodbogota@viviendatotal.co
  • Vivienda Total AV Villas             → mesa2.avvillas@viviendatotal.co
  • Estados avanzados Banco Bogotá       → legalizaciones@viviendatotal.co
  • Avalúos AV Villas                    → avaluos@viviendatotal.co
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.resilience import with_retry

logger = logging.getLogger(__name__)
_settings = get_settings()

# ─── Plantilla HTML del email de perfil de prospecto ─────────────────────────
_PLANTILLA_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <style>
    body  {{ font-family: Arial, sans-serif; color: #222; margin: 0; padding: 0; background: #f4f4f4; }}
    .card {{ max-width: 600px; margin: 30px auto; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.12); }}
    .hdr  {{ background: #003087; color: #fff; padding: 20px 28px; }}
    .hdr h1 {{ margin: 0; font-size: 20px; }}
    .hdr p  {{ margin: 4px 0 0; font-size: 13px; opacity: .85; }}
    .body {{ padding: 24px 28px; }}
    .field{{ display: flex; align-items: flex-start; margin-bottom: 12px; }}
    .lbl  {{ width: 180px; font-weight: bold; color: #555; font-size: 13px; flex-shrink: 0; }}
    .val  {{ font-size: 14px; color: #111; }}
    .badge{{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
    .alta {{ background: #d4edda; color: #155724; }}
    .media{{ background: #fff3cd; color: #856404; }}
    .baja {{ background: #f8d7da; color: #721c24; }}
    .docs {{ background: #f9f9f9; border-left: 4px solid #003087; padding: 12px 16px; margin-top: 8px; border-radius: 4px; }}
    .docs ul{{ margin: 6px 0; padding-left: 18px; }}
    .docs li{{ margin-bottom: 4px; font-size: 13px; }}
    .ftr  {{ background: #f4f4f4; text-align: center; padding: 14px; font-size: 11px; color: #888; }}
    hr    {{ border: none; border-top: 1px solid #eee; margin: 16px 0; }}
  </style>
</head>
<body>
<div class="card">
  <div class="hdr">
    <h1>🏦 Nuevo Prospecto — {outsourcing}</h1>
    <p>Generado automáticamente · {fecha}</p>
  </div>
  <div class="body">
    <div class="field"><span class="lbl">📱 Teléfono</span><span class="val">{telefono}</span></div>
    <div class="field"><span class="lbl">👤 Nombre</span><span class="val">{nombre}</span></div>
    <div class="field"><span class="lbl">🏢 Sector</span><span class="val">{sector}</span></div>
    <div class="field"><span class="lbl">🏛️ Banco Seleccionado</span><span class="val">{banco}</span></div>
    <div class="field"><span class="lbl">💳 Producto</span><span class="val">{producto}</span></div>
    <div class="field"><span class="lbl">💰 Ingresos Est.</span><span class="val">{ingresos}</span></div>
    <div class="field">
      <span class="lbl">⭐ Prioridad</span>
      <span class="val"><span class="badge {clase_prioridad}">{prioridad}</span></span>
    </div>
    <hr>
    <div class="field"><span class="lbl">📝 Análisis IA</span><span class="val">{resumen_ia}</span></div>
    {objeciones_html}
    <hr>
    <div class="docs">
      <strong>📋 Documentos solicitados al prospecto:</strong>
      <ul>{docs_lista}</ul>
    </div>
    <hr>
    <div class="field"><span class="lbl">🕐 Primer contacto</span><span class="val">{primer_contacto}</span></div>
    <div class="field"><span class="lbl">🔗 Origen</span><span class="val">{fuente}</span></div>
  </div>
  <div class="ftr">Ecosistema Omnicanal Financiero · asesoriafinan7@gmail.com · {fecha}</div>
</div>
</body>
</html>
"""


def _clase_prioridad(p: str) -> str:
    mapping = {"ALTA": "alta", "MEDIA": "media", "BAJA": "baja", "DESCALIFICADO": "baja"}
    return mapping.get(p.upper(), "media")


def _build_html(
    prospecto_data: Dict[str, Any],
    docs: List[str],
    outsourcing: str,
) -> str:
    p = prospecto_data
    objeciones = p.get("objeciones", [])
    objs_html = ""
    if objeciones:
        items = "".join(f"<li>{o}</li>" for o in objeciones)
        objs_html = f'<div class="field"><span class="lbl">⚠️ Objeciones</span><span class="val"><ul style="margin:0;padding-left:16px;">{items}</ul></span></div>'

    docs_lista = "".join(f"<li>{d}</li>" for d in docs) if docs else "<li>Estándar del producto</li>"

    ingresos_fmt = (
        f"${p.get('ingresos_estimados_cop', 0):,.0f} COP"
        if p.get("ingresos_estimados_cop")
        else "No especificado"
    )

    return _PLANTILLA_HTML.format(
        outsourcing=outsourcing,
        fecha=datetime.now().strftime("%d/%m/%Y %H:%M"),
        telefono=p.get("telefono", "N/D"),
        nombre=p.get("nombre", "Desconocido"),
        sector=p.get("sector", "Desconocido"),
        banco=p.get("banco", "No especificado"),
        producto=p.get("producto", "No especificado"),
        ingresos=ingresos_fmt,
        prioridad=p.get("prioridad", "MEDIA"),
        clase_prioridad=_clase_prioridad(p.get("prioridad", "MEDIA")),
        resumen_ia=p.get("resumen_ia", "Sin análisis disponible."),
        objeciones_html=objs_html,
        docs_lista=docs_lista,
        primer_contacto=p.get("primer_contacto", datetime.now().strftime("%d/%m/%Y %H:%M")),
        fuente=p.get("fuente", "WhatsApp Inbound"),
    )


# ─── Servicio SMTP ────────────────────────────────────────────────────────────

class EmailRouter:
    """
    Envía perfiles de prospectos a los outsourcings por SMTP Gmail.
    Usar SMTP_PASS = Contraseña de Aplicación de Google (16 caracteres).
    """

    def __init__(self) -> None:
        self._smtp_host = getattr(_settings, "smtp_host", "smtp.gmail.com")
        self._smtp_port = int(getattr(_settings, "smtp_port", 465))
        self._smtp_user = getattr(_settings, "smtp_user", "asesoriafinan7@gmail.com")
        self._smtp_pass = getattr(_settings, "smtp_pass", "")
        self._use_ssl   = self._smtp_port == 465

        if not self._smtp_pass:
            logger.warning(
                "SMTP_PASS no configurado. Los emails NO se enviarán. "
                "Crea una Contraseña de Aplicación en myaccount.google.com/apppasswords"
            )

    def _crear_mensaje(
        self,
        destinatario: str,
        asunto: str,
        cuerpo_html: str,
        cuerpo_texto: str,
    ) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"]    = f"Ecosistema Financiero <{self._smtp_user}>"
        msg["To"]      = destinatario
        msg["Reply-To"]= self._smtp_user

        msg.attach(MIMEText(cuerpo_texto, "plain", "utf-8"))
        msg.attach(MIMEText(cuerpo_html,  "html",  "utf-8"))
        return msg

    @with_retry(max_attempts=3, backoff_base=5.0, exceptions=(smtplib.SMTPException, OSError))
    def _enviar(self, destinatario: str, msg: MIMEMultipart) -> None:
        if not self._smtp_pass:
            logger.warning("Email NO enviado (sin SMTP_PASS): %s", destinatario)
            return
        if self._use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, context=context, timeout=20) as server:
                server.login(self._smtp_user, self._smtp_pass)
                server.sendmail(self._smtp_user, destinatario, msg.as_bytes())
        else:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.login(self._smtp_user, self._smtp_pass)
                server.sendmail(self._smtp_user, destinatario, msg.as_bytes())

    def enviar_perfil_expertos(
        self,
        prospecto_data: Dict[str, Any],
        docs_requeridos: List[str],
    ) -> bool:
        """Envía perfil a Expertos (AV Villas Libranza/Consumo) → 2expertos.bogota@gmail.com"""
        destinatario = "2expertos.bogota@gmail.com"
        nombre = prospecto_data.get("nombre", "Prospecto")
        producto = prospecto_data.get("producto", "Libranza")
        asunto = f"[PERFIL] {nombre} – {producto} AV Villas – {datetime.now().strftime('%d/%m/%Y')}"

        html  = _build_html(prospecto_data, docs_requeridos, "EXPERTOS")
        texto = (
            f"NUEVO PROSPECTO — EXPERTOS\n"
            f"Nombre:  {prospecto_data.get('nombre')}\n"
            f"Tel:     {prospecto_data.get('telefono')}\n"
            f"Sector:  {prospecto_data.get('sector')}\n"
            f"Banco:   {prospecto_data.get('banco', 'AV Villas')}\n"
            f"Producto:{producto}\n"
            f"Docs:    {', '.join(docs_requeridos)}\n"
        )

        try:
            msg = self._crear_mensaje(destinatario, asunto, html, texto)
            self._enviar(destinatario, msg)
            logger.info("✅ Perfil enviado a EXPERTOS: %s → %s", nombre, destinatario)
            return True
        except Exception as exc:
            logger.error("❌ Error enviando perfil a EXPERTOS: %s", exc, exc_info=True)
            return False

    def enviar_perfil_vivienda_total(
        self,
        prospecto_data: Dict[str, Any],
        docs_requeridos: List[str],
        banco_key: str,
        estado_avanzado: Optional[str] = None,
    ) -> bool:
        """
        Envía perfil a Vivienda Total.
        Selecciona la mesa correcta según el banco.
        Si estado_avanzado está en los triggers, usa el email avanzado.
        """
        from app.core.business_rules import get_rules_engine
        engine = get_rules_engine()
        routing = engine.get_routing("HIPOTECARIO", banco_key)

        # Decidir destinatario
        if estado_avanzado and routing.trigger_avanzado:
            if any(t.lower() in estado_avanzado.lower() for t in routing.trigger_avanzado):
                destinatario = routing.email_avanzado or routing.email_principal
            else:
                destinatario = routing.email_principal
        else:
            destinatario = routing.email_principal

        if not destinatario:
            logger.error("No hay email destino para Vivienda Total banco=%s", banco_key)
            return False

        nombre = prospecto_data.get("nombre", "Prospecto")
        banco_display = banco_key.replace("_", " ").title()
        asunto = (
            f"[HIPOTECARIO] {nombre} – {banco_display} – "
            f"{datetime.now().strftime('%d/%m/%Y')}"
        )

        html  = _build_html(prospecto_data, docs_requeridos, f"VIVIENDA TOTAL ({banco_display})")
        texto = (
            f"NUEVO PROSPECTO HIPOTECARIO — VIVIENDA TOTAL\n"
            f"Banco:   {banco_display}\n"
            f"Nombre:  {nombre}\n"
            f"Tel:     {prospecto_data.get('telefono')}\n"
            f"Docs:    {', '.join(docs_requeridos)}\n"
            f"Análisis:{prospecto_data.get('resumen_ia', 'N/D')}\n"
        )

        try:
            msg = self._crear_mensaje(destinatario, asunto, html, texto)
            self._enviar(destinatario, msg)
            logger.info("✅ Perfil hipotecario enviado a Vivienda Total: %s → %s", nombre, destinatario)
            return True
        except Exception as exc:
            logger.error("❌ Error enviando perfil a Vivienda Total: %s", exc, exc_info=True)
            return False

    def enviar_alerta_cambio_estado(
        self,
        negocio_id: str,
        nombre_cliente: str,
        banco: str,
        seccion_anterior: str,
        seccion_nueva: str,
        estado_nuevo: str,
        tipo: str,  # "aprobado" | "rechazado" | "avance"
    ) -> bool:
        """
        Envía alerta de cambio de estado de WiCapital al outsourcing correspondiente.
        Usado cuando ocurre un evento crítico en el crédito.
        """
        from app.core.business_rules import get_rules_engine
        engine = get_rules_engine()

        # Para hipotecarios, determinar si usar email avanzado
        routing = engine.get_routing("LIBRANZA", banco)
        destinatario = routing.email_avanzado or routing.email_principal
        if not destinatario:
            return False  # WiCapital scraper, no hay email que notificar

        emoji = "✅" if tipo == "aprobado" else ("❌" if tipo == "rechazado" else "🔄")
        asunto = f"{emoji} [{tipo.upper()}] Crédito {negocio_id} – {nombre_cliente}"
        cuerpo = (
            f"<h2>Cambio de Estado en WiCapital</h2>"
            f"<p><b>ID Negocio:</b> {negocio_id}</p>"
            f"<p><b>Cliente:</b> {nombre_cliente}</p>"
            f"<p><b>Antes:</b> {seccion_anterior}</p>"
            f"<p><b>Ahora:</b> {seccion_nueva} — {estado_nuevo}</p>"
            f"<p><b>Fecha:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>"
        )

        try:
            msg = self._crear_mensaje(destinatario, asunto, cuerpo, cuerpo)
            self._enviar(destinatario, msg)
            return True
        except Exception as exc:
            logger.error("Error enviando alerta cambio estado: %s", exc)
            return False
