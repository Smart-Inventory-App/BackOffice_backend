from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import get_current_admin
from app.models.admin import AdminUser

router = APIRouter(prefix="/admin/ml", tags=["admin-ml"])

_INTERNAL_HEADERS = {"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN}


async def _ml(method: str, path: str, **kwargs):
    url = f"{settings.ML_SERVICE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await getattr(client, method)(url, headers=_INTERNAL_HEADERS, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"ML service error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


class TrainRequest(BaseModel):
    model_type: Literal["gbm", "xgboost", "lightgbm", "svr"] = "gbm"
    data_source: Literal["real", "test"] = "real"
    segment: Literal["individuo", "comercio"] = "individuo"


@router.get("/models")
async def list_models(_: AdminUser = Depends(get_current_admin)):
    return await _ml("get", "/models/")


@router.post("/models/train")
async def train_model(body: TrainRequest = TrainRequest(), _: AdminUser = Depends(get_current_admin)):
    return await _ml("post", "/models/train", json=body.model_dump())


@router.get("/models/{version_id}/feature-importances")
async def get_feature_importances(version_id: str, _: AdminUser = Depends(get_current_admin)):
    return await _ml("get", f"/models/{version_id}/feature-importances")


@router.post("/models/{version_id}/activate")
async def activate_model(version_id: str, _: AdminUser = Depends(get_current_admin)):
    return await _ml("post", f"/models/{version_id}/activate")
