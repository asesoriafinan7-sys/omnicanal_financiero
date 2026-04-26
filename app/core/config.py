"""
Configuración central del ecosistema omnicanal financiero.
Lee secretos desde GCP Secret Manager en producción y desde variables de entorno localmente.
"""
import os
import logging
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ─── Sectores económicos soportados ──────────────────────────────────────────
SECTORES_ECONOMICOS = [
    "SALUD",
    "EDUCACION",
    "FUERZAS_MILITARES",
    "POLICIA_NACIONAL",
    "GOBIERNO",
    "EMPRESAS_PRIVADAS",
    "PENSIONADOS",
    "INDEPENDIENTES",
    "SECTOR_ENERGETICO",
    "SECTOR_PETROLERO",
    "SECTOR_MINERO",
    "SECTOR_FINANCIERO",
    "SECTOR_TECNOLOGIA",
    "SECTOR_CONSTRUCCION",
    "SECTOR_AGROPECUARIO",
]

# ─── Productos financieros soportados ────────────────────────────────────────
PRODUCTOS_FINANCIEROS = [
    "LIBRANZA",
    "CONSUMO",
    "COMPRA_CARTERA",
    "MICROFINANZAS",
]

# ─── Prioridades de prospecto ────────────────────────────────────────────────
PRIORIDADES = {
    "ALTA": 1,
    "MEDIA": 2,
    "BAJA": 3,
    "DESCALIFICADO": 4,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── GCP ──────────────────────────────────────────────────────────────────
    google_cloud_project: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "mi-proyecto-gcp")
    gcp_region: str = "us-central1"

    # ── Meta / WhatsApp Cloud API ─────────────────────────────────────────────
    meta_phone_number_id: str = os.environ.get("META_PHONE_NUMBER_ID", "")
    meta_waba_id: str = os.environ.get("META_WABA_ID", "")
    meta_access_token: str = os.environ.get("META_ACCESS_TOKEN", "")
    meta_verify_token: str = os.environ.get("META_VERIFY_TOKEN", "secure_verify_token_2025")
    meta_api_version: str = "v19.0"

    # ── Meta Ads API ──────────────────────────────────────────────────────────
    meta_ads_account_id: str = os.environ.get("META_ADS_ACCOUNT_ID", "")
    meta_ads_access_token: str = os.environ.get("META_ADS_ACCESS_TOKEN", "")

    # ── Vertex AI ────────────────────────────────────────────────────────────
    vertex_ai_location: str = "us-central1"
    llama_endpoint_id: str = os.environ.get("LLAMA_ENDPOINT_ID", "gemini-1.5-flash-001-001-001")
    mistral_endpoint_id: str = os.environ.get("MISTRAL_ENDPOINT_ID", "mistral-large@2411")

    # ── Google Sheets CRM ─────────────────────────────────────────────────────
    gsheets_credentials_json: str = os.environ.get("GSHEETS_CREDENTIALS_JSON", "")
    gsheets_spreadsheet_id: str = os.environ.get("GSHEETS_SPREADSHEET_ID", "")
    gsheets_prospectos_tab: str = "Prospectos"
    gsheets_campanas_tab: str = "Campanas"
    gsheets_conversiones_tab: str = "Conversiones"
    gsheets_wicapital_tab: str = "WiCapital"

    # ── WiCapital CRM Outsourcing ─────────────────────────────────────────────
    wicapital_login_url: str = os.environ.get(
        "WICAPITAL_LOGIN_URL", "https://sfwservicescrm.cloud/CRM_WELL_2022/index.php"
    )
    wicapital_user: str = os.environ.get("WICAPITAL_USER", "")
    wicapital_pass: str = os.environ.get("WICAPITAL_PASS", "")
    firestore_collection: str = os.environ.get("FIRESTORE_COLLECTION", "wicapital_estados")

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = os.environ.get("APP_ENV", "development")
    app_debug: bool = os.environ.get("APP_DEBUG", "false").lower() == "true"
    secret_manager_prefix: str = "omnicanal"
    cron_secret: str = os.environ.get("CRON_SECRET", "local_secret_2025")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def whatsapp_api_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}/{self.meta_phone_number_id}/messages"

    @property
    def meta_ads_api_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}/act_{self.meta_ads_account_id}"


def _load_secret_from_gcp(project_id: str, secret_name: str, version: str = "latest") -> str:
    """Carga un secreto desde GCP Secret Manager. Falla silenciosamente en desarrollo."""
    try:
        from google.cloud import secretmanager  # type: ignore

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/{version}"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception as exc:
        logger.warning("No se pudo cargar el secreto '%s' desde GCP: %s", secret_name, exc)
        return ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton de configuración. En producción enriquece con GCP Secret Manager."""
    cfg = Settings()

    if cfg.is_production:
        project = cfg.google_cloud_project
        # Sobrescribe tokens sensibles desde Secret Manager
        meta_token = _load_secret_from_gcp(project, f"{cfg.secret_manager_prefix}-meta-access-token")
        if meta_token:
            cfg = cfg.model_copy(update={"meta_access_token": meta_token})

        ads_token = _load_secret_from_gcp(project, f"{cfg.secret_manager_prefix}-meta-ads-access-token")
        if ads_token:
            cfg = cfg.model_copy(update={"meta_ads_access_token": ads_token})

        tg_token = _load_secret_from_gcp(project, f"{cfg.secret_manager_prefix}-telegram-bot-token")
        if tg_token:
            cfg = cfg.model_copy(update={"telegram_bot_token": tg_token})

        sheets_creds = _load_secret_from_gcp(project, f"{cfg.secret_manager_prefix}-gsheets-credentials")
        if sheets_creds:
            cfg = cfg.model_copy(update={"gsheets_credentials_json": sheets_creds})

        cron_sec = _load_secret_from_gcp(project, f"{cfg.secret_manager_prefix}-cron-secret")
        if cron_sec:
            cfg = cfg.model_copy(update={"cron_secret": cron_sec})

    return cfg
