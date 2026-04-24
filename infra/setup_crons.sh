#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_crons.sh — Configuración de orquestación externa con Cloud Scheduler
# Orquesta el scraping de WiCapital en el Ecosistema Omnicanal v3.0
# ─────────────────────────────────────────────────────────────────────────────

# --- CONFIGURACIÓN (Reemplazar con sus valores reales) ---
URL_APP="https://omnicanal-financiero-TU_ID_RANDOM-uc.a.run.app/api/v1/wicapital/sync"
CRON_SECRET="local_secret_2025" # Debe coincidir con Secret Manager: omnicanal-cron-secret
TIMEZONE="America/Bogota"
# --------------------------------------------------------

echo "🚀 Iniciando configuración de Cloud Scheduler para WiCapital..."

# Helper para crear jobs de forma consistente
create_job() {
    NAME=$1
    SCHEDULE=$2
    echo "  -> Creando job: $NAME ($SCHEDULE)"
    gcloud scheduler jobs create http "$NAME" \
        --schedule="$SCHEDULE" \
        --uri="$URL_APP" \
        --http-method=POST \
        --headers="X-Cron-Secret=$CRON_SECRET" \
        --time-zone="$TIMEZONE" \
        --location=us-central1
}

# --- LUNES A VIERNES (MAÑANA) ---
create_job "wicapital-sync-0835" "35 8 * * 1-5"
create_job "wicapital-sync-0945" "45 9 * * 1-5"
create_job "wicapital-sync-1100" "0 11 * * 1-5"
create_job "wicapital-sync-1230" "30 12 * * 1-5"

# --- LUNES A VIERNES (TARDE/NOCHE) ---
create_job "wicapital-sync-1430" "30 14 * * 1-5"
create_job "wicapital-sync-1545" "45 15 * * 1-5"
create_job "wicapital-sync-1720" "20 17 * * 1-5"
create_job "wicapital-sync-1815" "15 18 * * 1-5"
create_job "wicapital-sync-1930" "30 19 * * 1-5"

# --- SÁBADOS ---
create_job "wicapital-sync-sat-0900" "0 9 * * 6"
create_job "wicapital-sync-sat-1130" "30 11 * * 6"

echo "✅ Configuración de orquestación finalizada."
echo "⚠️ RECUERDA: El secreto '$CRON_SECRET' debe ser subido a GCP Secret Manager como 'omnicanal-cron-secret'."
