#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_gcp.sh — Script de aprovisionamiento inicial de infraestructura GCP
#
# Ejecutar UNA SOLA VEZ desde una máquina con gcloud CLI autenticado.
# Prerrequisitos:
#   1. gcloud auth login
#   2. Tener permisos de Owner o roles: roles/iam.admin, roles/run.admin
#
# Uso:
#   chmod +x infra/setup_gcp.sh
#   ./infra/setup_gcp.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Variables — EDITAR ANTES DE EJECUTAR ────────────────────────────────────
PROJECT_ID="mi-proyecto-gcp-123"
REGION="us-central1"
SERVICE_ACCOUNT_NAME="omnicanal-sa"
SERVICE_NAME="omnicanal-financiero"
FIRESTORE_COLLECTION="wicapital_estados"

echo "=== Configurando GCP para: ${PROJECT_ID} ==="

# ── 1. Establecer proyecto activo ────────────────────────────────────────────
gcloud config set project "${PROJECT_ID}"

# ── 2. Habilitar APIs requeridas ──────────────────────────────────────────────
echo "Habilitando APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    firestore.googleapis.com \
    aiplatform.googleapis.com \
    sheets.googleapis.com \
    drive.googleapis.com \
    containerregistry.googleapis.com

echo "✅ APIs habilitadas."

# ── 3. Crear Service Account ─────────────────────────────────────────────────
echo "Creando Service Account: ${SERVICE_ACCOUNT_NAME}..."
gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
    --display-name="Omnicanal Financiero SA" \
    --description="Service Account para el ecosistema omnicanal financiero" \
    2>/dev/null || echo "Service Account ya existe, continuando..."

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# ── 4. Asignar roles al Service Account ──────────────────────────────────────
echo "Asignando roles IAM..."
ROLES=(
    "roles/datastore.user"              # Firestore
    "roles/secretmanager.secretAccessor" # Secret Manager
    "roles/aiplatform.user"             # Vertex AI
    "roles/run.invoker"                 # Cloud Run (invocación)
    "roles/storage.objectViewer"        # GCS (para artefactos)
    "roles/logging.logWriter"           # Cloud Logging
)

for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${ROLE}" \
        --quiet
    echo "  ✅ ${ROLE}"
done

# ── 5. Crear key JSON del SA (para desarrollo local) ──────────────────────────
echo "Generando clave JSON del Service Account..."
gcloud iam service-accounts keys create "./infra/service_account.json" \
    --iam-account="${SA_EMAIL}"
echo "✅ Clave guardada en ./infra/service_account.json"
echo "⚠️  NUNCA subir este archivo a control de versiones."

# ── 6. Crear base de datos Firestore ─────────────────────────────────────────
echo "Inicializando Firestore (modo Native)..."
gcloud firestore databases create \
    --region="${REGION}" \
    --type=firestore-native \
    2>/dev/null || echo "Firestore ya existe, continuando..."

# ── 7. Crear secretos en Secret Manager ──────────────────────────────────────
echo "Creando secretos en Secret Manager..."
SECRETS=(
    "omnicanal-meta-access-token"
    "omnicanal-meta-ads-access-token"
    "omnicanal-telegram-bot-token"
    "omnicanal-gsheets-credentials"
)

for SECRET in "${SECRETS[@]}"; do
    gcloud secrets create "${SECRET}" \
        --replication-policy="automatic" \
        2>/dev/null || echo "  Secreto '${SECRET}' ya existe."
    echo "  ✅ Secreto preparado: ${SECRET}"
done

echo ""
echo "=== SIGUIENTE PASO: Cargar valores en los secretos ==="
echo "Ejemplo:"
echo "  echo -n 'TU_TOKEN_AQUI' | gcloud secrets versions add omnicanal-meta-access-token --data-file=-"
echo ""

# ── 8. Configurar Cloud Run Service ──────────────────────────────────────────
echo "El servicio Cloud Run se desplegará automáticamente via Cloud Build."
echo "Para despliegue manual después de construir la imagen:"
echo ""
echo "  gcloud run deploy ${SERVICE_NAME} \\"
echo "    --image=gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest \\"
echo "    --region=${REGION} \\"
echo "    --platform=managed \\"
echo "    --allow-unauthenticated \\"
echo "    --service-account=${SA_EMAIL} \\"
echo "    --memory=2Gi --cpu=2 \\"
echo "    --min-instances=0 --max-instances=10"
echo ""
echo "=== ✅ Aprovisionamiento GCP completado ==="
