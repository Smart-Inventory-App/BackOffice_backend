"""
Puerta de salida del backoffice hacia el coordinador de OCR.

Está acá y no dentro de un router porque hay dos consumidores con necesidades
distintas: el panel de administración, que diagnostica el módulo y firma un token
propio de servicio, y el flujo de usuario, que reenvía el token de quien escanea
para que la cuota y la propiedad del job queden a su nombre. Lo único que
comparten es cómo se habla con el coordinador, y eso es lo que vive en este
módulo.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import HTTPException, UploadFile, status
from jose import jwt

from app.core.config import settings

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
DOC_TYPES = {"product_label", "expiry_date", "receipt", "barcode"}

# Mismo tope que aplica image_storage_service a la carpeta temporal: rechazarlo
# acá ahorra el viaje de ida y da un mensaje de error más claro.
MAX_BYTES = 10 * 1024 * 1024


def service_token() -> str:
    """
    Token con el que el backoffice se identifica ante el coordinador de OCR.

    Se usa cuando no hay un usuario detrás de la petición —el panel de
    administración, la limpieza de temporales—: se firma uno propio y de vida
    corta con el mismo secreto compartido de la plataforma, así que no hay un
    segundo esquema de credenciales que mantener.
    """
    return jwt.encode(
        {
            "sub": "backoffice",
            "email": "backoffice@smartinventory",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


async def request(method: str, path: str, token: Optional[str] = None, **kwargs):
    """
    Una llamada al coordinador, con el error traducido a HTTP de este servicio.

    `token` es el del usuario cuando lo hay: el coordinador cuenta la cuota por
    `sub` y guarda el dueño del job, así que reenviarlo es lo que hace que un
    usuario no consuma el cupo de otro ni pueda leer su escaneo.
    """
    url = f"{settings.OCR_SERVICE_URL}{path}"
    headers = {"Authorization": f"Bearer {token or service_token()}"}
    try:
        async with httpx.AsyncClient(timeout=settings.OCR_TIMEOUT_SECONDS) as client:
            r = await getattr(client, method)(url, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"OCR service error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=detail(r), headers=_retry_after(r))
    return r.json()


def detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    body = payload.get("detail") if isinstance(payload, dict) else None
    return str(body) if body else response.text[:200]


def _retry_after(response: httpx.Response) -> Optional[dict]:
    """
    Conserva el `Retry-After` de un 429.

    Sin esto el usuario ve «superaste el límite» sin saber cuánto esperar, que es
    justamente el dato que el coordinador se tomó el trabajo de calcular.
    """
    valor = response.headers.get("retry-after")
    return {"Retry-After": valor} if valor else None


async def read_image(file: UploadFile) -> bytes:
    """Valida formato y tamaño antes de que la foto salga a la red."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        accepted = ", ".join(sorted(ALLOWED_CONTENT_TYPES))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Formato '{file.content_type}' no permitido. Formatos aceptados: {accepted}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo está vacío")
    if len(content) > MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"La imagen supera el máximo de {MAX_BYTES // (1024 * 1024)} MB",
        )

    return content


def validate_doc_type(doc_type: str) -> str:
    if doc_type not in DOC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"doc_type inválido. Valores aceptados: {', '.join(sorted(DOC_TYPES))}",
        )
    return doc_type
