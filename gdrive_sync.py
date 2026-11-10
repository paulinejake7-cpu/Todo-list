"""Синхронизация списка задач с Google Диском.

Нужен файл credentials.json (OAuth-клиент типа «Приложение для ПК»), см. README.md.
Приложение использует область доступа drive.file: оно видит только тот файл,
который создало само, остальные файлы на вашем Диске ему недоступны.
"""
import io
import json
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
REMOTE_NAME = "todo_tasks.json"


class SyncError(Exception):
    pass


def find_credentials(*dirs) -> Path | None:
    for d in dirs:
        p = Path(d) / "credentials.json"
        if p.exists():
            return p
    return None


def _service(data_dir: Path, credentials_path: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise SyncError(
            "Не установлены библиотеки Google. Выполните:\n"
            "pip install google-api-python-client google-auth-oauthlib"
        ) from e

    token_path = Path(data_dir) / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)  # откроется браузер для входа
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_remote_id(service) -> str | None:
    res = service.files().list(
        q=f"name = '{REMOTE_NAME}' and trashed = false",
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _download(service, file_id: str) -> dict:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    req = service.files().get_media(fileId=file_id)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return json.loads(buf.getvalue().decode("utf-8")).get("tasks", {})


def _upload(service, tasks: dict, file_id: str | None):
    from googleapiclient.http import MediaIoBaseUpload

    payload = json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        service.files().create(
            body={"name": REMOTE_NAME, "mimeType": "application/json"},
            media_body=media,
            fields="id",
        ).execute()


def sync(local_tasks: dict, data_dir: Path, credentials_path: Path, merge_fn) -> dict:
    """Скачать файл с Диска, слить с локальными задачами, загрузить результат обратно.
    Возвращает объединённый набор задач."""
    service = _service(data_dir, credentials_path)
    file_id = _find_remote_id(service)
    remote = _download(service, file_id) if file_id else {}
    merged = merge_fn(local_tasks, remote)
    _upload(service, merged, file_id)
    return merged
