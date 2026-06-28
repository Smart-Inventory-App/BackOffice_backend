from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.core.database import Base


class AdminUser(Base):
    """Whitelist de administradores del backoffice.

    Las credenciales (contraseña) NO se guardan acá: las gestiona el servicio
    `auth` (tabla `auth_users`), igual que para los usuarios de la app. Esta tabla
    solo determina QUÉ emails son administradores autorizados.
    """

    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # False hasta que el admin define su contraseña en el primer inicio de sesión.
    password_set = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
