import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()
id_sheet = os.getenv('GSHEETS_SPREADSHEET_ID')
creds_path = os.getenv('GSHEETS_CREDENTIALS_JSON')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

def probar():
    print('--- Iniciando prueba de conexion con Service Account ---')
    print(f'ID detectado: {id_sheet}')
    print(f'JSON path: {creds_path}')
    try:
        # Cargamos credenciales usando service account en lugar de default()
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets().get(spreadsheetId=id_sheet).execute()
        print('\n[OK] CONEXION EXITOSA!')
        print(f'Hoja detectada: {sheet.get("properties", {}).get("title")}')
    except Exception as e:
        print(f'\n[ERROR]: {e}')

if __name__ == "__main__":
    probar()