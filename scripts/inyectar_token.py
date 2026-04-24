import os
import sys
import subprocess
from typing import Optional

# Intentamos importar dependencias para validación Meta
try:
    import httpx
except ImportError:
    try:
        import requests as httpx
    except ImportError:
        httpx = None

# ANSI Colors para consola profesional
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'
BLUE = '\033[94m'
BOLD = '\033[1m'

# TOKEN PERMANENTE (Se inyecta una vez y se sanitiza tras el éxito)
TOKEN = "REDACTED_AFTER_SUCCESSFUL_INJECTION"

def log_success(msg: str):
    print(f"{GREEN}{BOLD}[OK]{RESET} {msg}")

def log_error(msg: str):
    print(f"{RED}{BOLD}[ERROR]{RESET} {msg}", file=sys.stderr)

def log_info(msg: str):
    print(f"{BLUE}{BOLD}[INFO]{RESET} {msg}")

def update_env(token: str):
    """Actualización segura de .env evitando leaks."""
    log_info("Paso 1: Sincronizando .env local...")
    env_path = ".env"
    lines = []
    found = False
    
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
    with open(env_path, "w", encoding="utf-8") as f:
        for line in lines:
            if line.strip().startswith("META_ACCESS_TOKEN="):
                f.write(f"META_ACCESS_TOKEN={token}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"\nMETA_ACCESS_TOKEN={token}\n")
    
    log_success("Archivo .env actualizado y persistido.")

def inject_gcp(token: str):
    """Inyección en Secret Manager vía stdin para evitar logs de comandos."""
    log_info("Paso 2: Escalando secreto a GCP Secret Manager...")
    try:
        # Detectar gcloud path en Windows
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        gcloud_candidates = [
            os.path.join(local_app_data, 'Google', 'Cloud SDK', 'google-cloud-sdk', 'bin', 'gcloud.cmd'),
            "gcloud.cmd",
            "gcloud"
        ]
        
        gcloud_cmd = "gcloud"
        for cand in gcloud_candidates:
            if os.path.exists(cand) or subprocess.run(["where", cand], capture_output=True).returncode == 0:
                gcloud_cmd = cand
                break

        # Ejecución con paso de datos por stdin
        process = subprocess.run(
            [gcloud_cmd, "secrets", "versions", "add", "omnicanal-meta-access-token", "--data-file=-"],
            input=token.encode(),
            capture_output=True,
            check=True
        )
        log_success("Versión de secreto añadida correctamente en GCP.")
    except subprocess.CalledProcessError as e:
        log_error(f"Fallo en gcloud execution: {e.stderr.decode().strip()}")
        raise
    except Exception as e:
        log_error(f"Error inesperado en integración GCP: {str(e)}")
        raise

def validate_meta(token: str):
    """Validación de integridad del token contra el Graph API."""
    log_info("Paso 3: Validando integridad del token en Meta Graph API v18.0...")
    if not httpx:
        log_error("No se encontró httpx ni requests. Saltando validación API.")
        return

    url = f"https://graph.facebook.com/v18.0/me?access_token={token}"
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            log_success(f"Token Verificado. App Identity: {data.get('name')} | ID: {data.get('id')}")
        else:
            log_error(f"Integridad de Token Comprometida: {resp.text}")
            raise Exception("Invalid Token Response")
    except Exception as e:
        log_error(f"Error de conectividad API: {str(e)}")
        raise

def self_sanitize():
    """Sanitización de código fuente para eliminar el secreto del archivo físico."""
    log_info("Finalizando: Ejecutando protocolo de sanitización de código...")
    script_path = __file__
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Ofuscación/Eliminación del token
        sanitized_content = content.replace(f'TOKEN = "{TOKEN}"', 'TOKEN = "REDACTED_AFTER_SUCCESSFUL_INJECTION"')
        
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(sanitized_content)
        log_success("Script sanitizado satisfactoriamente.")
    except Exception as e:
        log_error(f"Error en sanitización: {str(e)}")

def main():
    print(f"\n{BOLD}=== OMNICANAL FINANCIERO: SECURITY TOKEN INJECTOR ==={RESET}\n")
    try:
        update_env(TOKEN)
        inject_gcp(TOKEN)
        validate_meta(TOKEN)
        log_success("\n[SUCCESS] INFRAESTRUCTURA ACTUALIZADA EXITOSAMENTE")
        self_sanitize()
    except Exception:
        log_error("\n[FAIL] DESPLIEGUE DE TOKEN ABORTADO POR ERRORES CRÍTICOS")
        sys.exit(1)

if __name__ == "__main__":
    main()
