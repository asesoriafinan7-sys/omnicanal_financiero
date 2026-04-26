# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — Ecosistema Omnicanal Financiero
# Base: Python 3.11 slim + Chromium headless para Selenium (WiCapital scraper)
# Target: Google Cloud Run (puerto 8080, escalado a cero)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# Instalar dependencias del sistema para Chromium + Selenium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    curl \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf-xlib-2.0-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Variables de entorno para Chromium headless
ENV CHROMIUM_FLAGS="--headless --no-sandbox --disable-dev-shm-usage --disable-gpu"
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Directorio de trabajo
WORKDIR /app

# Copiar e instalar dependencias Python primero (capa cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY . .

# Usuario no root para seguridad
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
RUN chown -R appuser:appgroup /app
USER appuser

# Puerto Cloud Run
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/api/v1/health || exit 1

# Comando de inicio
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--log-level", "info", "--access-log"]

# --- INYECCION DE EMERGENCIA ---
USER root
RUN mkdir -p /app/app/core && \
    echo '{"horario_atencion": {"inicio": "00:00", "fin": "23:59"}, "dias_laborales": [0, 1, 2, 3, 4, 5, 6], "mensaje_fuera_horario": "Inconveniente técnico.", "estado_sistema": "activo"}' > /app/app/core/business_rules.json && \
    chown -R appuser:appgroup /app/app/core
USER appuser
# -------------------------------
