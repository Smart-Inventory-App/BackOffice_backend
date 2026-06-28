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
