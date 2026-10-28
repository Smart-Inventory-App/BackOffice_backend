import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin, get_current_user
from app.models.admin import AdminUser
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit
from app.schemas.admin import (
    AdminCreate,
    AdminResponse,
    CheckEmailRequest,
    CheckEmailResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    ResetPasswordRequest,
    ResetTokenResponse,
    SetPasswordRequest,
    VerifyCodeRequest,
    TokenResponse,
)

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])

_AUTH = settings.AUTH_SERVICE_URL.rstrip("/")


def _auth_request(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        with httpx.Client(timeout=15) as client:
            return client.request(method, f"{_AUTH}{path}", **kwargs)
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo contactar al servicio de autenticación",
        )


def _auth_post(path: str, payload: dict) -> httpx.Response:
    return _auth_request("POST", path, json=payload)


def _get_admin_row(email: str, db: Session) -> AdminUser | None:
    """Lee el mirror LOCAL de `admin_users` — misma base compartida que
    escribe `auth`, sin red de por medio. Solo las escrituras (alta de admin,
    set-password) van por HTTP contra `auth`, que es quien las origina."""
    return db.query(AdminUser).filter(AdminUser.email == email).first()


@router.post("/check", response_model=CheckEmailResponse)
def check_email(body: CheckEmailRequest, db: Session = Depends(get_db)):
    """Paso 1 del login: dado un email, indica si existe cuenta, si es admin y
    si ya tiene contraseña. Cualquier cuenta de `auth_users` (no solo admins)
    puede loguearse."""
    admin = _get_admin_row(body.email, db)
    if admin and admin.is_active:
        return CheckEmailResponse(exists=True, is_admin=True, password_set=admin.password_set)

    user = db.query(AuthUser).filter(AuthUser.email == body.email).first()
    if user and user.is_active:
        has_password = user.has_password is not False
        return CheckEmailResponse(exists=True, is_admin=False, password_set=has_password, google_only=not has_password)

    return CheckEmailResponse(exists=False, is_admin=False, password_set=False)


_REGISTER_ERROR_MESSAGES = {
    "auth.email_already_registered": "Ya existe una cuenta con ese email. Iniciá sesión.",
    "auth.password_too_short": "La contraseña debe tener al menos 8 caracteres.",
    "auth.password_missing_uppercase": "La contraseña debe incluir al menos una mayúscula.",
    "auth.password_missing_lowercase": "La contraseña debe incluir al menos una minúscula.",
    "auth.password_missing_digit": "La contraseña debe incluir al menos un número.",
}


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest):
    """Alta de una cuenta nueva (usuario común, no admin) desde el backoffice.
    Delega enteramente en `auth`, igual que el registro de la app — es la
    misma `auth_users`. Para ser admin, otro admin lo agrega después desde
    /admin/auth/admins."""
    r = _auth_post("/auth/register", {"email": body.email, "password": body.password})
    if r.status_code == 201:
        return
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    raise HTTPException(status_code=r.status_code, detail=_REGISTER_ERROR_MESSAGES.get(detail, detail or "No se pudo crear la cuenta"))


_RECOVERY_ERROR_MESSAGES = {
    "auth.invalid_code": "El código no es válido.",
    "auth.invalid_or_expired_code": "El código no es válido o venció. Pedí uno nuevo.",
    "auth.too_many_attempts": "Demasiados intentos. Pedí un código nuevo.",
    "auth.invalid_or_expired_token": "El enlace venció. Volvé a empezar.",
    "auth.password_too_short": "La contraseña debe tener al menos 8 caracteres.",
    "auth.password_same_as_current": "La contraseña nueva no puede ser igual a la actual.",
}


def _recovery_error(r: httpx.Response, fallback: str) -> HTTPException:
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    return HTTPException(status_code=r.status_code, detail=_RECOVERY_ERROR_MESSAGES.get(detail, fallback))


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest):
    """Recuperar (o crear, en una cuenta de Google) la contraseña: `auth` manda un código por mail. Responde lo mismo
    exista o no la cuenta, para no revelar quién tiene una."""
    r = _auth_post("/auth/forgot-password", {"email": body.email})
    if r.status_code == 200:
        return {"detail": "sent"}
    raise _recovery_error(r, "No se pudo enviar el código.")


@router.post("/verify-code", response_model=ResetTokenResponse)
def verify_code(body: VerifyCodeRequest):
    r = _auth_post("/auth/verify-code", {"email": body.email, "code": body.code})
    if r.status_code == 200:
        return ResetTokenResponse(reset_token=r.json()["reset_token"])
    raise _recovery_error(r, "No se pudo verificar el código.")


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(body: ResetPasswordRequest):
    r = _auth_post("/auth/reset-password", {"reset_token": body.reset_token, "new_password": body.new_password})
    if r.status_code in (200, 204):
        return
    raise _recovery_error(r, "No se pudo cambiar la contraseña.")


@router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
def set_password(body: SetPasswordRequest):
    """Primer inicio de sesión de un admin whitelisteado: `auth` valida la
    whitelist, las reglas de contraseña, y registra la cuenta en
    `auth_users` — es quien escribe tanto `auth_users` como `admin_users`."""
    r = _auth_post(
        "/auth/admin/set-password",
        {"email": body.email, "password": body.password, "invite_token": body.invite_token},
    )
    if r.status_code == 204:
        return
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    if r.status_code == 409 and detail == "auth.email_already_registered":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existía una cuenta con ese email. Iniciá sesión con tu contraseña actual.",
        )
    if r.status_code == 409:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este administrador ya definió su contraseña")
    if r.status_code == 403 and detail == "auth.invalid_invite":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El código de invitación no es válido o venció. Pedile uno nuevo a un administrador.",
        )
    if r.status_code == 403:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado: el email no es administrador")
    raise HTTPException(status_code=r.status_code, detail=detail or "No se pudo definir la contraseña")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Login: delega la verificación de credenciales a `auth`. Los admins
    deben tener contraseña definida; cualquier otra cuenta activa de la app
    puede loguearse directamente (ya tiene contraseña desde la app)."""
    admin = _get_admin_row(body.email, db)
    if admin:
        if not admin.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta deshabilitada")
        if not admin.password_set:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Debés definir tu contraseña antes de iniciar sesión",
            )
    else:
        user = db.query(AuthUser).filter(AuthUser.email == body.email).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta deshabilitada")

    r = _auth_post("/auth/login", {"email": body.email, "password": body.password, "firebase_token": ""})
    if r.status_code == 200:
        auth_user = db.query(AuthUser).filter(AuthUser.email == body.email).first()
        db.add(
            AdminActivityLog(
                event_type="login",
                actor_email=body.email,
                target_user_id=str(auth_user.id) if auth_user else None,
                target_email=body.email,
                description=f"{body.email} inició sesión en el backoffice"
                + (" (administrador)" if admin else ""),
            )
        )
        db.commit()
        return TokenResponse(access_token=r.json()["access_token"])
    if r.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    if r.status_code == 403:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta deshabilitada")
    raise HTTPException(status_code=r.status_code, detail="No se pudo iniciar sesión")


@router.get("/me", response_model=MeResponse)
def me(db: Session = Depends(get_db), current: AuthUser = Depends(get_current_user)):
    admin = _get_admin_row(current.email, db)
    is_admin = bool(admin and admin.is_active)
    return MeResponse(
        id=str(current.id),
        email=current.email,
        full_name=admin.full_name if admin else None,
        is_admin=is_admin,
        is_active=bool(current.is_active),
    )


@router.get("/admins", response_model=list[AdminResponse])
def list_admins(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """Lectura local del mirror — igual costo que antes, sin red."""
    return db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()


@router.post("/admins", response_model=AdminResponse, status_code=status.HTTP_201_CREATED)
def create_admin(
    data: AdminCreate,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Alta de admin: la escritura la hace `auth` (dueño de `admin_users`),
    reenviando el bearer para que `auth` valide ahí también que el caller es
    admin."""
    if _get_admin_row(data.email, db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un administrador con ese email",
        )

    r = _auth_request(
        "POST",
        "/auth/admin/admins",
        json={"email": data.email, "full_name": data.full_name},
        headers={"Authorization": authorization},
    )
    if r.status_code == 409:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un administrador con ese email")
    if r.status_code != 201:
        raise HTTPException(status_code=r.status_code, detail="No se pudo crear el administrador")

    existing_user = db.query(AuthUser).filter(AuthUser.email == data.email).first()
    if existing_user:
        db.add(
            AdminRoleAudit(
                user_id=str(existing_user.id),
                changed_by=current_admin.email,
                action="granted",
            )
        )
        db.commit()

    return r.json()
