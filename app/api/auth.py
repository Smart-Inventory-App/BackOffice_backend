import httpx
from jose import JWTError, jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin, get_current_user
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, UserProfile
from app.models.audit import AdminActivityLog, AdminRoleAudit
from app.schemas.admin import (
    AdminCreate,
    AdminResponse,
    ChangePasswordRequest,
    CheckEmailRequest,
    CheckEmailResponse,
    ForgotPasswordRequest,
    GoogleLoginRequest,
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

    if (admin and not admin.is_active) or (user and not user.is_active):
        return CheckEmailResponse(exists=False, is_admin=False, password_set=False, disabled=True)

    return CheckEmailResponse(exists=False, is_admin=False, password_set=False)


# Lo que se le dice a quien intenta entrar con una cuenta deshabilitada
DISABLED_ACCOUNT_MESSAGE = "El usuario del mail ingresado se encuentra deshabilitado. Por favor, ingresa con otra cuenta"

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
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DISABLED_ACCOUNT_MESSAGE)
        if not admin.password_set:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Debés definir tu contraseña antes de iniciar sesión",
            )
    else:
        user = db.query(AuthUser).filter(AuthUser.email == body.email).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No existe una cuenta con ese email")
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DISABLED_ACCOUNT_MESSAGE)

    r = _auth_post("/auth/login", {"email": body.email, "password": body.password, "firebase_token": ""})
    if r.status_code == 200:
        auth_user = db.query(AuthUser).filter(AuthUser.email == body.email).first()
        db.add(
            AdminActivityLog(
                event_type="login",
                actor_email=body.email,
                target_user_id=str(auth_user.id) if auth_user else None,
                target_email=body.email,
                description=f"{body.email} inició sesión en el dashboard"
                + (" (administrador)" if admin else ""),
            )
        )
        db.commit()
        return TokenResponse(access_token=r.json()["access_token"])
    if r.status_code == 401:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    if r.status_code == 403:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DISABLED_ACCOUNT_MESSAGE)
    raise HTTPException(status_code=r.status_code, detail="No se pudo iniciar sesión")


# Los mismos mensajes que muestra la app del celular cuando falla el ingreso con Google
_GOOGLE_ERROR_MESSAGES = {
    "google_not_configured": "El inicio de sesión con Google no está disponible por el momento",
    "google_unreachable": "No se pudo contactar a Google",
    "google_invalid_token": "Token de Google inválido",
    "google_email_not_verified": "El email de Google no está verificado",
    "account_disabled": DISABLED_ACCOUNT_MESSAGE,
}


@router.post("/google", response_model=TokenResponse)
def google_login(body: GoogleLoginRequest, db: Session = Depends(get_db)):
    """Ingreso y registro con Google, igual que en la app: `auth` verifica el idToken (que lo haya emitido Google para
    nuestro cliente Web), crea la cuenta si es la primera vez y devuelve el token de siempre. Quien tiene una cuenta
    deshabilitada o es un administrador dado de baja no entra."""
    r = _auth_post("/auth/google", {"id_token": body.id_token})
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        code = detail.removeprefix("auth.") if isinstance(detail, str) else None
        raise HTTPException(
            status_code=r.status_code,
            detail=_GOOGLE_ERROR_MESSAGES.get(code, "Error al iniciar sesión con Google"),
        )

    access_token = r.json()["access_token"]
    try:
        email = jwt.decode(access_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]).get("email")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Error al iniciar sesión con Google")
    admin = _get_admin_row(email, db)
    if admin and not admin.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DISABLED_ACCOUNT_MESSAGE)

    auth_user = db.query(AuthUser).filter(AuthUser.email == email).first()
    db.add(
        AdminActivityLog(
            event_type="login",
            actor_email=email,
            target_user_id=str(auth_user.id) if auth_user else None,
            target_email=email,
            description=f"{email} inició sesión con Google en el dashboard" + (" (administrador)" if admin else ""),
        )
    )
    db.commit()
    return TokenResponse(access_token=access_token)


@router.get("/me", response_model=MeResponse)
def me(db: Session = Depends(get_db), current: AuthUser = Depends(get_current_user)):
    admin = _get_admin_row(current.email, db)
    is_admin = bool(admin and admin.is_active)
    # Solo el nombre y el apellido que la persona tiene en su perfil (si no los tiene, nada): lo mismo para un administrador
    # que para un usuario común
    profile = db.query(UserProfile).filter(UserProfile.user_id == str(current.id)).first()
    full_name = " ".join(filter(None, [profile.first_name if profile else None, profile.last_name if profile else None])).strip()
    return MeResponse(
        id=str(current.id),
        email=current.email,
        full_name=full_name or None,
        is_admin=is_admin,
        is_active=bool(current.is_active),
        must_change_password=bool(current.must_change_password),
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    authorization: str = Header(...),
    _: AuthUser = Depends(get_current_user),
):
    """Cambia la contraseña de quien tiene la sesión iniciada: lo hace `auth` con el mismo token. Es lo que cierra el
    «tenés que cambiarla» de un administrador que ingresó con la contraseña inicial que le dieron."""
    r = _auth_request(
        "POST",
        "/auth/change-password",
        json={"current_password": body.current_password, "new_password": body.new_password},
        headers={"Authorization": authorization},
    )
    if r.status_code == 204:
        return
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    messages = {
        **_REGISTER_ERROR_MESSAGES,
        "auth.invalid_credentials": "La contraseña actual no es correcta.",
        "auth.password_same_as_current": "La contraseña nueva no puede ser igual a la actual.",
    }
    raise HTTPException(status_code=r.status_code, detail=messages.get(detail, "No se pudo cambiar la contraseña."))


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

    existing_user = db.query(AuthUser).filter(AuthUser.email == data.email).first()
    if not existing_user and not data.initial_password:
        # Sin cuenta hace falta una contraseña inicial para pasársela a quien va a ser administrador: el panel la pide.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ese email todavía no tiene cuenta: definí una contraseña inicial para pasársela.",
        )

    payload = {"email": data.email, "full_name": data.full_name}
    if not existing_user:
        payload["initial_password"] = data.initial_password
    r = _auth_request("POST", "/auth/admin/admins", json=payload, headers={"Authorization": authorization})
    if r.status_code == 409:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un administrador con ese email")
    if r.status_code == 400:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_REGISTER_ERROR_MESSAGES.get(detail, "La contraseña inicial no es válida."),
        )
    if r.status_code != 201:
        raise HTTPException(status_code=r.status_code, detail="No se pudo crear el administrador")

    account = existing_user or db.query(AuthUser).filter(AuthUser.email == data.email).first()
    if account:
        db.add(
            AdminRoleAudit(
                user_id=str(account.id),
                changed_by=current_admin.email,
                action="granted",
            )
        )
        db.commit()

    return {**r.json(), "account_created": existing_user is None}
