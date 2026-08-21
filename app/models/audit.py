from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.core.database import Base


class UserStatusAudit(Base):
    """Registro de auditoría de cambios de estado (habilitado/deshabilitado) de
    los usuarios de la app, realizados desde el backoffice."""

    __tablename__ = "admin_user_status_audit"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)   # auth_users.id (UUID como texto)
    changed_by = Column(String, nullable=False)            # email del admin que hizo el cambio
    is_active = Column(Boolean, nullable=False)            # nuevo estado tras el cambio
    reason = Column(String, nullable=True)                 # motivo (opcional)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminRoleAudit(Base):
    """Registro de auditoría de otorgar/quitar el rol de administrador a un
    usuario de la app, realizados desde el backoffice."""

    __tablename__ = "admin_role_audit"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)   # auth_users.id (UUID como texto)
    changed_by = Column(String, nullable=False)            # email del admin que hizo el cambio
    action = Column(String, nullable=False)                # "granted" | "revoked"
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminActivityLog(Base):
    """Registro general de actividad del backoffice (logins, borrado de
    productos por un admin, y otros eventos que no encajan en las tablas de
    auditoría más específicas de arriba)."""

    __tablename__ = "admin_activity_log"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False)             # "login" | "product_deleted"
    actor_email = Column(String, nullable=True)              # quién hizo la acción
    target_user_id = Column(String, nullable=True)           # a quién/qué afecta (auth_users.id)
    target_email = Column(String, nullable=True)
    description = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
