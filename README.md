# 🏦 Ecosistema Omnicanal Financiero — Colombia

**Plataforma de automatización omnicanal y multiproducto para el sector financiero colombiano.**
Gestiona prospectos de Libranza, Consumo y Compra de Cartera con IA generativa, WhatsApp Cloud API, Meta Ads y CRM en Google Sheets.

---

## 📁 Estructura del Proyecto

```
omnicanal_financiero/
│
├── main.py                          ← Punto de entrada FastAPI (Cloud Run)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml               ← Desarrollo local
├── .env.example                     ← Template de variables de entorno
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py                ← Settings (GCP Secret Manager + .env)
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py               ← Esquemas Pydantic (todas las entidades)
│   ├── routers/
│   │   ├── __init__.py
│   │   └── api.py                   ← Todos los endpoints FastAPI
│   └── services/
│       ├── __init__.py
│       ├── ai_engine.py             ← Vertex AI: Llama 3.3 + Mistral Large 3
│       ├── crm_sync.py              ← Google Sheets CRM + WiCapital + Telegram
│       ├── marketing_ads.py         ← Meta Ads API (neuromarketing)
│       └── whatsapp_service.py      ← WhatsApp Cloud API + extractor de chats
│
├── scripts/
│   ├── cargar_base_datos.py         ← CLI: Importar bases Excel/CSV al CRM
│   ├── enviar_campana_wsp.py        ← CLI: Campañas masivas de WhatsApp
│   └── auditar_chats.py             ← CLI: Auditoría masiva de chats con IA
│
└── infra/
    ├── cloudbuild.yaml              ← Pipeline CI/CD Cloud Build
    └── setup_gcp.sh                 ← Script de aprovisionamiento GCP (1 vez)
```

---

## 🚀 Inicio Rápido

### 1. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus credenciales reales
```

### 2. Instalar dependencias

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 3. Correr localmente

```bash
uvicorn main:app --reload --port 8080
```

Acceder a la documentación interactiva: **http://localhost:8080/docs**

---

## 🤖 Capa de IA (Vertex AI MaaS)

| Modelo | Uso | Latencia |
|--------|-----|----------|
| **Llama 3.3 70B** | Perfilamiento de prospectos, extracción JSON, auditoría de chats | Media |
| **Mistral Large 3** | Respuestas conversacionales del chatbot WhatsApp | Baja |

### Ejemplo de perfilamiento con Llama 3.3:

```json
POST /api/v1/ia/perfilar?mensaje=Soy%20médico%20del%20HUV%20y%20necesito%20un%20crédito
```

Respuesta:
```json
{
  "califica": true,
  "producto_detectado": "LIBRANZA",
  "sector_economico": "SALUD",
  "prioridad": "ALTA",
  "ingresos_estimados_cop": 8500000,
  "confianza_score": 0.92,
  "respuesta_sugerida": "¡Hola doctor! Para profesionales del sector salud..."
}
```

---

## 📲 Webhook de WhatsApp

Configurar en Meta for Developers:
- **URL del webhook**: `https://TU_CLOUD_RUN_URL/api/v1/webhook/whatsapp`
- **Token de verificación**: Valor de `META_VERIFY_TOKEN` en `.env`
- **Campos suscritos**: `messages`

**Flujo automático por cada mensaje entrante:**
1. Recepción → respuesta 200 inmediata (< 5s requerimiento Meta)
2. Perfilamiento con **Llama 3.3** → JSON de perfil
3. Guardado en **Google Sheets CRM** (upsert por teléfono)
4. Respuesta conversacional con **Mistral Large 3**
5. Alerta **Telegram** si prioridad = ALTA

---

## 📊 Endpoints Principales

### WhatsApp
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/api/v1/webhook/whatsapp` | Verificación Meta |
| `POST` | `/api/v1/webhook/whatsapp` | Mensajes entrantes |
| `POST` | `/api/v1/crm/retomar-cliente` | Plantilla HSM retoma |
| `POST` | `/api/v1/crm/campana-masiva` | Envío masivo |
| `POST` | `/api/v1/crm/auditar-chat` | Auditoría de chat con IA |

### Meta Ads
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/campanas/lanzar` | Flujo completo Campaña→AdSet→Ad |
| `GET` | `/api/v1/campanas/listar` | Listar campañas activas |
| `GET` | `/api/v1/campanas/{id}/metricas` | Métricas de performance |

### CRM
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/crm/importar-base` | Importar y segmentar base de datos |
| `GET` | `/api/v1/prospectos/prioridad/{p}` | Prospectos por prioridad |
| `GET` | `/api/v1/prospectos/sector/{s}` | Prospectos por sector |
| `PATCH` | `/api/v1/prospectos/estado` | Actualizar estado CRM |

### WiCapital
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/api/v1/wicapital/sync` | Ciclo scraping manual |

---

## 🛠️ Scripts de Línea de Comandos

### Importar base de datos (cualquier sector)
```bash
# Excel de clientes de salud
python scripts/cargar_base_datos.py --archivo clientes_hospital.xlsx

# CSV mixto con detección automática de sector
python scripts/cargar_base_datos.py --archivo base_empresas.csv --tab "Q1_2025"

# Dry-run para validar sin subir
python scripts/cargar_base_datos.py --archivo test.xlsx --dry-run
```

### Campañas WhatsApp masivas
```bash
# Desde CSV
python scripts/enviar_campana_wsp.py --csv retoma_enero.csv --plantilla RETOMA_LIBRANZA

# Cliente individual
python scripts/enviar_campana_wsp.py --telefono +573001234567 --nombre "Ana Gómez" \
  --producto COMPRA_CARTERA --tasa 1.2

# Dry-run
python scripts/enviar_campana_wsp.py --csv clientes.csv --dry-run
```

### Auditoría de chats
```bash
# Carpeta completa
python scripts/auditar_chats.py --carpeta chats/ --exportar auditoria_Q1.xlsx

# Solo análisis local (sin IA)
python scripts/auditar_chats.py --carpeta chats/ --solo-local
```

---

## ☁️ Despliegue en Google Cloud Run

### Aprovisionamiento inicial (solo 1 vez)
```bash
chmod +x infra/setup_gcp.sh
./infra/setup_gcp.sh
```

### CI/CD automático via Cloud Build
```bash
# Conectar repositorio en Cloud Build Console y usar cloudbuild.yaml
# Cualquier push a `main` dispara: Build → Push → Deploy
```

### Despliegue manual
```bash
gcloud builds submit --config infra/cloudbuild.yaml \
  --substitutions=_REGION=us-central1
```

---

## 🔐 Seguridad

- **GCP Secret Manager**: Almacena todos los tokens sensibles en producción.
- **No-root container**: El Dockerfile usa usuario `appuser` sin privilegios.
- **CORS**: Restringido a dominios propios en `APP_ENV=production`.
- **`.gitignore`**: Excluye `.env`, JSONs de credenciales y bases de datos.

---

## 📣 Sectores Económicos Soportados

`SALUD` · `EDUCACION` · `FUERZAS_MILITARES` · `POLICIA_NACIONAL` · `GOBIERNO`
`EMPRESAS_PRIVADAS` · `PENSIONADOS` · `INDEPENDIENTES` · `SECTOR_ENERGETICO`
`SECTOR_PETROLERO` · `SECTOR_MINERO` · `SECTOR_FINANCIERO` · `SECTOR_TECNOLOGIA`
`SECTOR_CONSTRUCCION` · `SECTOR_AGROPECUARIO`

---

## 📋 Productos Financieros

| Producto | Descripción |
|----------|-------------|
| `LIBRANZA` | Descuento directo de nómina, hasta 120 meses |
| `CONSUMO` | Crédito personal, desembolso 48h |
| `COMPRA_CARTERA` | Consolidación de deudas, ahorro hasta 40% |
| `MICROFINANZAS` | Desde $2M COP para independientes/agro |

---

*Versión 2.0.0 — Ecosistema Omnicanal Financiero Colombia*
