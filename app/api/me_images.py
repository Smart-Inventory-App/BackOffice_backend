"""Imagen de un producto: se sube como archivo y el servicio de almacenamiento devuelve su dirección."""
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from app.core.config import settings
from app.core.security import get_current_user
from app.core.upstream import error_detail
from app.models.app_models import AuthUser

router = APIRouter(prefix="/me/products", tags=["me"])

ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp")
FOLDER = "tpp/products"


@router.post("/image")
async def upload_product_image(
    request: Request,
    file: UploadFile = File(...),
    _: AuthUser = Depends(get_current_user),
):
    """Sube la foto de un producto y devuelve `{ "url": ... }` para guardarla en el producto.

    El archivo viaja al servicio de almacenamiento con el mismo token del usuario; acá no se guarda nada. Se validan el
    tipo y el tamaño antes de subirlo, para avisar enseguida y no después de transferir una foto enorme.
    """
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La imagen tiene que ser JPG, PNG o WebP.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo está vacío.")
    if len(content) > settings.MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"La imagen pesa más de {settings.MAX_IMAGE_MB} MB.",
        )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.IMAGE_STORAGE_URL}/storage/upload",
                headers={"Authorization": request.headers.get("authorization", "")},
                files={"file": (file.filename or "product.jpg", content, file.content_type)},
                data={"folder": FOLDER},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"No se pudo subir la imagen: {e}")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=error_detail(response))
    return {"url": response.json()["url"]}
