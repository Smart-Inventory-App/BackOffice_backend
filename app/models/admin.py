from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.core.database import Base


class AdminUser(Base):
    """Mirror de solo lectura de `admin_users`, tabla owneada por el servicio
    `auth` (ahí se crea y se escribe — ver Auth/app/routes/admin.py). Este
    backoffice NUNCA escribe esta tabla directamente: todas las altas y el
    "set password" van por HTTP contra `auth`. Se mantiene el modelo local
    solo para lecturas rápidas (p. ej. `get_current_admin`, que corre en cada
    request admin) sin pegarle a `auth` por red en cada una.
    """

    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # False hasta que el admin define su contraseña en el primer inicio de sesión.
    password_set = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
