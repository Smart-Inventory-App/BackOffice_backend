import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.core.security import get_current_admin
from app.core.upstream import error_detail
from app.models.admin import AdminUser

router = APIRouter(prefix="/admin/alerts", tags=["admin-alerts"])

_INTERNAL_HEADERS = {"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN}


async def _app(method: str, path: str, **kwargs):
    url = f"{settings.APP_BACKEND_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await getattr(client, method)(url, headers=_INTERNAL_HEADERS, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"App backend error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=error_detail(r))
    return r.json()


@router.post("/run")
async def trigger_alerts_all(_: AdminUser = Depends(get_current_admin)):
    """Lanza el chequeo de alertas ML para todos los usuarios de forma inmediata."""
    return await _app("post", "/internal/alerts/run")


@router.post("/run/{user_id}")
async def trigger_alerts_user(user_id: str, _: AdminUser = Depends(get_current_admin)):
    """Lanza el chequeo de alertas ML para un usuario específico de forma inmediata."""
    return await _app("post", f"/internal/alerts/run/{user_id}")
