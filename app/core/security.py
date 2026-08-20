from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.admin import AdminUser
from app.models.app_models import AuthUser

_bearer = HTTPBearer()


def _decode_email(credentials: HTTPAuthorizationCredentials) -> str:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado",
    )
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        email = payload.get("email")
        if not email:
            raise cred_exc
        return email
    except JWTError:
        raise cred_exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AuthUser:
    """Valida el JWT emitido por el servicio `auth` y exige que el email tenga una
    cuenta activa en `auth_users`. No requiere ser administrador: cualquier usuario
    de la app puede autenticarse en el backoffice con esta dependencia."""
    email = _decode_email(credentials)
    user = db.query(AuthUser).filter(AuthUser.email == email).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado: la cuenta no existe o está deshabilitada",
        )
    return user


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AdminUser:
    """Valida el JWT emitido por el servicio `auth` (secreto compartido) y exige
    que el email pertenezca a la whitelist de administradores activos.

    Es la misma validación local que usa app_backend, más el chequeo de admin.
    """
    email = _decode_email(credentials)
    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not admin or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado: el usuario no es administrador",
        )
    return admin
