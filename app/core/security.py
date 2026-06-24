from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.admin import AdminUser

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_admin_token(admin_id: str, email: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ADMIN_JWT_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(admin_id), "email": email, "role": "admin", "exp": expire},
        settings.ADMIN_JWT_SECRET,
        algorithm=settings.ADMIN_JWT_ALGORITHM,
    )


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AdminUser:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado",
    )
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.ADMIN_JWT_SECRET,
            algorithms=[settings.ADMIN_JWT_ALGORITHM],
        )
        if payload.get("role") != "admin" or "sub" not in payload:
            raise cred_exc
        admin_id = payload["sub"]
    except JWTError:
        raise cred_exc

    admin = db.query(AdminUser).filter(AdminUser.id == int(admin_id)).first()
    if not admin or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrador no autorizado o deshabilitado",
        )
    return admin
