from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from jose import jwt
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import get_current_admin
from app.models.admin import AdminUser

router = APIRouter(prefix="/admin/ocr", tags=["admin-ocr"])

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
DOC_TYPES = {"product_label", "expiry_date", "receipt", "barcode"}
MAX_BYTES = 10 * 1024 * 1024


def _service_token() -> str:
    """
    Token con el que el backoffice se identifica ante el coordinador de OCR.

    A diferencia de la app no hay token de usuario que reenviar: se firma uno
    propio y de vida corta con el mismo secreto compartido de la plataforma, así
    que no hay un segundo esquema de credenciales que mantener.
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


async def _ocr(method: str, path: str, **kwargs):
    url = f"{settings.OCR_SERVICE_URL}{path}"
    headers = {"Authorization": f"Bearer {_service_token()}"}
    try:
        async with httpx.AsyncClient(timeout=settings.OCR_TIMEOUT_SECONDS) as client:
            r = await getattr(client, method)(url, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"OCR service error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=_detail(r))
    return r.json()


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail) if detail else response.text[:200]


class EngineStatus(BaseModel):
    name: str
    url: str = ""
    available: bool = False
    version: str = ""
    replicas: List[str] = Field(
        default_factory=list,
        description="Instancias del motor. PaddleOCR va replicado; el panel tiene que mostrarlas.",
    )


class OCRStatus(BaseModel):
    available: bool
    checks: dict = Field(
        default_factory=dict,
        description="Redis, motor configurado y decodificador de códigos, según el coordinador.",
    )
    engines: List[EngineStatus] = Field(default_factory=list)


@router.get("/status", response_model=OCRStatus)
async def ocr_status(_: AdminUser = Depends(get_current_admin)):
    """
    Estado del módulo de OCR para el panel de plataforma.

    No falla si el módulo está caído: eso es justamente lo que el panel tiene que
    poder mostrar, así que devuelve `available: false` con lo que haya podido
    averiguar.
    """
    checks, available = await _readyz()
    return OCRStatus(available=available, checks=checks, engines=await _engines())


@router.post("/scan")
async def scan(
    file: UploadFile = File(...),
    doc_type: str = Form(default="product_label"),
    engine: Optional[str] = Form(default=None),
    _: AdminUser = Depends(get_current_admin),
):
    """
    Procesa una foto y devuelve la lectura completa, sin encolar.

    Es sincrónico y devuelve todo lo que el módulo leyó —confianza por campo,
    texto legible, motor ganador, intentos— porque acá se diagnostica por qué una
    foto sale mal. `engine` permite forzar un motor para compararlos.

    La imagen no se guarda en ningún lado: va directo al coordinador, que la
    procesa en memoria.
    """
    if doc_type not in DOC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"doc_type inválido. Valores aceptados: {', '.join(sorted(DOC_TYPES))}",
        )

    content = await _read_image(file)

    data = {"doc_type": doc_type}
    if engine:
        data["engine"] = engine

    return await _ocr(
        "post",
        "/ocr/sync/upload",
        files={"file": (file.filename or "scan.jpg", content, file.content_type)},
        data=data,
    )


@router.post("/temp/purge")
async def purge_temp(_: AdminUser = Depends(get_current_admin)):
    """
    Borra las imágenes temporales de escaneo que quedaron sin descartar.

    El worker del OCR descarta la suya al terminar cada job, así que el caso
    normal ya está cubierto; esto limpia lo que queda cuando el proceso muere en
    medio de uno. Va contra el coordinador y no contra el storage: el storage es
    dependencia suya, no de este servicio.
    """
    return await _ocr("post", "/ocr/maintenance/purge-temp")


async def _readyz() -> tuple[dict, bool]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{settings.OCR_SERVICE_URL}/ocr/readyz")
        payload = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e)}, False

    # Un 503 del coordinador trae su diagnóstico dentro de `detail`: es
    # información para el panel, no un error que haya que tapar.
    if r.status_code == 503 and isinstance(payload, dict):
        payload = payload.get("detail", payload)

    return payload if isinstance(payload, dict) else {}, r.status_code == 200


async def _engines() -> List[EngineStatus]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{settings.OCR_SERVICE_URL}/ocr/engines")
        return [EngineStatus(**engine) for engine in r.json()] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, TypeError):
        return []


async def _read_image(file: UploadFile) -> bytes:
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
