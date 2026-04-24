"""
social_media_manager.py — Gestor de Publicaciones Orgánicas Multicanal.

Orquesta publicaciones orgánicas en:
  • Facebook / Instagram (Meta Graph API)
  • TikTok (Content Posting API v2)
  • LinkedIn (Share API v2)

La IA (Mistral Large 3) redacta los copys de forma dinámica
según sector, producto y formato solicitado.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.core.config import get_settings
from app.core.resilience import CB_META_API, with_retry
from app.services.ai_engine import generar_copy_organico_mistral

logger = logging.getLogger(__name__)
_settings = get_settings()

META_GRAPH_BASE = f"https://graph.facebook.com/{_settings.meta_api_version}"


# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class ContenidoPost:
    texto:    str
    sector:   str
    producto: str
    formato:  str          # "post" | "reel_guion" | "story" | "linkedin_articulo"
    imagen_url: Optional[str] = None
    video_url:  Optional[str] = None
    hashtags:   List[str]  = field(default_factory=list)


@dataclass
class ResultadoPublicacion:
    plataforma: str
    exito:      bool
    post_id:    Optional[str] = None
    url:        Optional[str] = None
    mensaje:    str = ""


# ─── FACEBOOK / INSTAGRAM (Meta Graph API) ───────────────────────────────────

class MetaOrganicClient:
    """
    Publica contenido orgánico en páginas de Facebook e Instagram vía Graph API.
    Requiere: META_ACCESS_TOKEN, META_PAGE_ID, META_IG_USER_ID en .env
    """

    def __init__(self) -> None:
        self._token   = _settings.meta_access_token
        self._page_id = getattr(_settings, "meta_page_id", "")
        self._ig_id   = getattr(_settings, "meta_ig_user_id", "")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @with_retry(max_attempts=3, backoff_base=3.0, exceptions=(requests.HTTPError, OSError))
    def _post_graph(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not CB_META_API.allow():
            raise RuntimeError("CircuitBreaker META_API abierto.")
        url  = f"{META_GRAPH_BASE}/{endpoint}"
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        CB_META_API.record_success()
        return resp.json()

    def publicar_facebook(self, contenido: ContenidoPost) -> ResultadoPublicacion:
        """Publica un post de texto (con imagen opcional) en la Página de Facebook."""
        if not self._page_id:
            return ResultadoPublicacion("FACEBOOK", False, mensaje="META_PAGE_ID no configurado.")
        try:
            payload: Dict[str, Any] = {
                "message": contenido.texto,
                "access_token": self._token,
            }
            if contenido.imagen_url:
                payload["link"] = contenido.imagen_url
            data = self._post_graph(f"{self._page_id}/feed", payload)
            post_id = data.get("id", "")
            logger.info("✅ Post publicado en Facebook: %s", post_id)
            return ResultadoPublicacion(
                "FACEBOOK", True,
                post_id=post_id,
                url=f"https://facebook.com/{post_id}",
                mensaje="Publicado en Facebook.",
            )
        except Exception as exc:
            CB_META_API.record_failure()
            logger.error("Error publicando en Facebook: %s", exc)
            return ResultadoPublicacion("FACEBOOK", False, mensaje=str(exc))

    def publicar_instagram(self, contenido: ContenidoPost) -> ResultadoPublicacion:
        """
        Publica en Instagram vía Content Publishing API.
        Si hay imagen_url → imagen; si no → caption solo (requiere imagen para IG).
        """
        if not self._ig_id:
            return ResultadoPublicacion("INSTAGRAM", False, mensaje="META_IG_USER_ID no configurado.")
        try:
            # Paso 1: Crear container de media
            media_payload: Dict[str, Any] = {
                "caption": contenido.texto,
                "access_token": self._token,
            }
            if contenido.imagen_url:
                media_payload["image_url"] = contenido.imagen_url
            else:
                # Sin imagen, IG no permite publicar solo texto
                return ResultadoPublicacion(
                    "INSTAGRAM", False,
                    mensaje="Instagram requiere imagen_url para publicar. Agrega una imagen.",
                )

            container = self._post_graph(f"{self._ig_id}/media", media_payload)
            container_id = container.get("id")
            if not container_id:
                raise ValueError("No se creó el container de media en IG.")

            # Paso 2: Publicar el container
            publish = self._post_graph(
                f"{self._ig_id}/media_publish",
                {"creation_id": container_id, "access_token": self._token},
            )
            post_id = publish.get("id", "")
            logger.info("✅ Post publicado en Instagram: %s", post_id)
            return ResultadoPublicacion(
                "INSTAGRAM", True,
                post_id=post_id,
                mensaje="Publicado en Instagram.",
            )
        except Exception as exc:
            CB_META_API.record_failure()
            logger.error("Error publicando en Instagram: %s", exc)
            return ResultadoPublicacion("INSTAGRAM", False, mensaje=str(exc))


# ─── TIKTOK (Content Posting API v2) ─────────────────────────────────────────

class TikTokClient:
    """
    Publica videos o fotos en TikTok usando Content Posting API v2.
    Requiere: TIKTOK_ACCESS_TOKEN, TIKTOK_OPEN_ID en .env
    """

    API_BASE = "https://open.tiktokapis.com/v2"

    def __init__(self) -> None:
        self._token   = getattr(_settings, "tiktok_access_token", "")
        self._open_id = getattr(_settings, "tiktok_open_id", "")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def publicar_video(self, contenido: ContenidoPost) -> ResultadoPublicacion:
        """
        Pública un Video/Reel en TikTok.
        video_url debe ser una URL pública accesible (puede ser GCS/S3).
        """
        if not self._token or not self._open_id:
            return ResultadoPublicacion(
                "TIKTOK", False,
                mensaje="TIKTOK_ACCESS_TOKEN o TIKTOK_OPEN_ID no configurados. "
                        "Ver: developers.tiktok.com para obtener las credenciales.",
            )
        if not contenido.video_url:
            return ResultadoPublicacion(
                "TIKTOK", False,
                mensaje="TikTok requiere video_url para publicar un video.",
            )
        try:
            payload = {
                "post_info": {
                    "title":            contenido.texto[:150],  # TikTok: máx 150 chars en título
                    "privacy_level":    "PUBLIC_TO_EVERYONE",
                    "disable_duet":     False,
                    "disable_stitch":   False,
                    "disable_comment":  False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source":    "PULL_FROM_URL",
                    "video_url": contenido.video_url,
                },
            }
            resp = requests.post(
                f"{self.API_BASE}/post/publish/video/init/",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data    = resp.json()
            pub_id  = data.get("data", {}).get("publish_id", "")
            logger.info("✅ Video publicado en TikTok: publish_id=%s", pub_id)
            return ResultadoPublicacion(
                "TIKTOK", True,
                post_id=pub_id,
                mensaje="Video enviado a TikTok para publicación.",
            )
        except Exception as exc:
            logger.error("Error publicando en TikTok: %s", exc)
            return ResultadoPublicacion("TIKTOK", False, mensaje=str(exc))

    def publicar_foto(self, contenido: ContenidoPost) -> ResultadoPublicacion:
        """Publica una foto/carrusel en TikTok (Photo Mode)."""
        if not self._token or not contenido.imagen_url:
            return ResultadoPublicacion(
                "TIKTOK", False,
                mensaje="Se requiere TIKTOK_ACCESS_TOKEN y imagen_url para Photo Mode.",
            )
        try:
            payload = {
                "post_info": {
                    "title":         contenido.texto[:150],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_comment": False,
                },
                "source_info": {
                    "source":    "PULL_FROM_URL",
                    "photo_cover_index": 0,
                    "photo_images":  [contenido.imagen_url],
                },
                "post_mode": "DIRECT_POST",
                "media_type": "PHOTO",
            }
            resp = requests.post(
                f"{self.API_BASE}/post/publish/content/init/",
                json=payload,
                headers=self._headers(),
                timeout=20,
            )
            resp.raise_for_status()
            data   = resp.json()
            pub_id = data.get("data", {}).get("publish_id", "")
            return ResultadoPublicacion("TIKTOK", True, post_id=pub_id, mensaje="Foto publicada en TikTok.")
        except Exception as exc:
            logger.error("Error publicando foto TikTok: %s", exc)
            return ResultadoPublicacion("TIKTOK", False, mensaje=str(exc))


# ─── LINKEDIN (Share API v2) ─────────────────────────────────────────────────

class LinkedInClient:
    """
    Publica contenido en LinkedIn (Página de Organización o Perfil Personal).
    Requiere: LINKEDIN_ACCESS_TOKEN, LINKEDIN_ORGANIZATION_ID o LINKEDIN_PERSON_ID en .env
    """

    API_BASE = "https://api.linkedin.com/v2"

    def __init__(self) -> None:
        self._token   = getattr(_settings, "linkedin_access_token", "")
        self._org_id  = getattr(_settings, "linkedin_organization_id", "")
        self._person_id = getattr(_settings, "linkedin_person_id", "")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type":  "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": "202304",
        }

    def _get_author(self) -> str:
        if self._org_id:
            return f"urn:li:organization:{self._org_id}"
        if self._person_id:
            return f"urn:li:person:{self._person_id}"
        raise ValueError("Configurar LINKEDIN_ORGANIZATION_ID o LINKEDIN_PERSON_ID en .env")

    @with_retry(max_attempts=3, backoff_base=3.0, exceptions=(requests.HTTPError,))
    def publicar_texto(self, contenido: ContenidoPost) -> ResultadoPublicacion:
        """Publica un post de texto en LinkedIn."""
        if not self._token:
            return ResultadoPublicacion(
                "LINKEDIN", False,
                mensaje="LINKEDIN_ACCESS_TOKEN no configurado. "
                        "Crear app en linkedin.com/developers.",
            )
        try:
            author = self._get_author()
            payload = {
                "author":     author,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": contenido.texto},
                        "shareMediaCategory": "NONE",
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            }
            resp = requests.post(
                f"{self.API_BASE}/ugcPosts",
                json=payload,
                headers=self._headers(),
                timeout=20,
            )
            resp.raise_for_status()
            post_id = resp.headers.get("x-restli-id", "")
            logger.info("✅ Post publicado en LinkedIn: %s", post_id)
            return ResultadoPublicacion(
                "LINKEDIN", True,
                post_id=post_id,
                mensaje="Publicado en LinkedIn.",
            )
        except Exception as exc:
            logger.error("Error publicando en LinkedIn: %s", exc)
            return ResultadoPublicacion("LINKEDIN", False, mensaje=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR MULTICANAL
# ═══════════════════════════════════════════════════════════════════════════════

class SocialMediaOrchestrator:
    """
    Orquesta publicaciones orgánicas en múltiples plataformas.
    Usa IA para generar el copy adaptado a cada formato.
    """

    def __init__(self) -> None:
        self._meta    = MetaOrganicClient()
        self._tiktok  = TikTokClient()
        self._linkedin = LinkedInClient()

    async def generar_y_publicar(
        self,
        sector: str,
        producto: str,
        banco: str = "",
        plataformas: List[str] | None = None,
        imagen_url: Optional[str] = None,
        video_url:  Optional[str] = None,
        tono: str = "profesional y cercano",
    ) -> Dict[str, ResultadoPublicacion]:
        """
        Genera el copy con IA y publica de forma paralela en las plataformas indicadas.

        plataformas: ["FACEBOOK", "INSTAGRAM", "TIKTOK", "LINKEDIN"] — None = todas.
        """
        plataformas = plataformas or ["FACEBOOK", "INSTAGRAM", "LINKEDIN"]
        resultados: Dict[str, ResultadoPublicacion] = {}

        # ── Generar copys en paralelo por formato ─────────────────────────────
        formato_por_plataforma = {
            "FACEBOOK":  "post",
            "INSTAGRAM": "post",
            "TIKTOK":    "reel_guion",
            "LINKEDIN":  "linkedin_articulo",
        }

        copys: Dict[str, str] = {}
        tareas = {
            p: generar_copy_organico_mistral(
                sector=sector, producto=producto, formato=formato_por_plataforma.get(p, "post"),
                banco=banco, tono=tono,
            )
            for p in plataformas
        }
        resultados_ia = await asyncio.gather(*tareas.values(), return_exceptions=True)
        for plataforma, res in zip(tareas.keys(), resultados_ia):
            copys[plataforma] = res if isinstance(res, str) else f"[Error IA: {res}]"

        # ── Publicar en cada plataforma ───────────────────────────────────────
        loop = asyncio.get_event_loop()

        for plataforma in plataformas:
            texto = copys.get(plataforma, "")
            contenido = ContenidoPost(
                texto=texto, sector=sector, producto=producto,
                formato=formato_por_plataforma.get(plataforma, "post"),
                imagen_url=imagen_url, video_url=video_url,
            )

            try:
                if plataforma == "FACEBOOK":
                    res = await loop.run_in_executor(None, self._meta.publicar_facebook, contenido)
                elif plataforma == "INSTAGRAM":
                    res = await loop.run_in_executor(None, self._meta.publicar_instagram, contenido)
                elif plataforma == "TIKTOK":
                    if video_url:
                        res = await loop.run_in_executor(None, self._tiktok.publicar_video, contenido)
                    else:
                        res = await loop.run_in_executor(None, self._tiktok.publicar_foto, contenido)
                elif plataforma == "LINKEDIN":
                    res = await loop.run_in_executor(None, self._linkedin.publicar_texto, contenido)
                else:
                    res = ResultadoPublicacion(plataforma, False, mensaje="Plataforma no soportada.")
            except Exception as exc:
                res = ResultadoPublicacion(plataforma, False, mensaje=str(exc))

            resultados[plataforma] = res
            logger.info(
                "Publicación %s en %s — Éxito: %s",
                plataforma, "OK" if res.exito else "FALLO", res.mensaje,
            )

        return resultados

    async def solo_generar_copy(
        self,
        sector: str,
        producto: str,
        banco: str = "",
        formatos: List[str] | None = None,
        tono: str = "profesional y cercano",
    ) -> Dict[str, str]:
        """
        Solo genera copys con IA sin publicar.
        Útil para revisión humana antes de publicar.
        """
        formatos = formatos or ["post", "reel_guion", "story", "linkedin_articulo"]
        tareas = {
            f: generar_copy_organico_mistral(sector, producto, formato=f, banco=banco, tono=tono)
            for f in formatos
        }
        resultados_ia = await asyncio.gather(*tareas.values(), return_exceptions=True)
        return {
            fmt: (res if isinstance(res, str) else f"[Error: {res}]")
            for fmt, res in zip(tareas.keys(), resultados_ia)
        }
