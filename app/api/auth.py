import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.schemas.admin import (
    AdminCreate,
    AdminResponse,
    CheckEmailRequest,
    CheckEmailResponse,
    LoginRequest,
    SetPasswordRequest,
    TokenResponse,
)

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])

_AUTH = settings.AUTH_SERVICE_URL.rstrip("/")


def _auth_post(path: str, payload: dict) -> httpx.Response:
    try:
        with httpx.Client(timeout=15) as client:
            return client.post(f"{_AUTH}{path}", json=payload)
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo contactar al servicio de autenticación",
        )


def _require_admin_row(email: str, db: Session) -> AdminUser:
    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not admin or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado: el email no es administrador",
        )
    return admin


@router.post("/check", response_model=CheckEmailResponse)
def check_email(body: CheckEmailRequest, db: Session = Depends(get_db)):
    """Paso 1 del login: dado un email, indica si es admin y si ya tiene contraseña."""
    admin = db.query(AdminUser).filter(AdminUser.email == body.email).first()
    if not admin or not admin.is_active:
        return CheckEmailResponse(is_admin=False, password_set=False)
    return CheckEmailResponse(is_admin=True, password_set=admin.password_set)


@router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
def set_password(body: SetPasswordRequest, db: Session = Depends(get_db)):
    """Primer inicio de sesión: el admin define su contraseña, que se registra en
    el servicio auth (igual que un usuario de la app)."""
    admin = _require_admin_row(body.email, db)
    if admin.password_set:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este administrador ya definió su contraseña",
        )

    r = _auth_post("/auth/register", {"email": body.email, "password": body.password})
    if r.status_code == 201:
        admin.password_set = True
        db.commit()
        return
    # El email ya tenía cuenta en auth (p. ej. era usuario de la app): lo damos por
    # válido y dejamos que inicie sesión con esa contraseña existente.
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    if r.status_code == 400 and detail == "auth.email_already_registered":
        admin.password_set = True
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existía una cuenta con ese email. Iniciá sesión con tu contraseña actual.",
        )
    raise HTTPException(status_code=r.status_code, detail=detail or "No se pudo definir la contraseña")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Login de admin: delega la verificación de credenciales al servicio auth y
    exige que el email esté en la whitelist de administradores."""
    admin = _require_admin_row(body.email, db)
    if not admin.password_set:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Debés definir tu contraseña antes de iniciar sesión",
        )

    r = _auth_post("/auth/login", {"email": body.email, "password": body.password, "firebase_token": ""})
    if r.status_code == 200:
        return TokenResponse(access_token=r.json()["access_token"])
    if r.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    if r.status_code == 403:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta deshabilitada")
    raise HTTPException(status_code=r.status_code, detail="No se pudo iniciar sesión")


@router.get("/me", response_model=AdminResponse)
def me(current: AdminUser = Depends(get_current_admin)):
    return current


@router.get("/admins", response_model=list[AdminResponse])
def list_admins(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    return db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()


@router.post("/admins", response_model=AdminResponse, status_code=status.HTTP_201_CREATED)
def create_admin(
    data: AdminCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    if db.query(AdminUser).filter(AdminUser.email == data.email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un administrador con ese email",
        )
    admin = AdminUser(email=data.email, full_name=data.full_name, password_set=False)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin
