from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, func

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


class OCRScanAudit(Base):
    """Un renglón por cada ejecución del módulo de OCR, sea de un usuario o del
    panel.

    Existe por una restricción del diseño: las fotos que se escanean **no se
    guardan en ningún lado** —el coordinador las sube como temporales y el worker
    las descarta al terminar—, así que sin esta tabla no queda rastro de que el
    módulo corrió. Es lo que permite responder después «cuántas veces escaneó
    este usuario», «cuánto tardó» y «con qué confianza salió», que es justamente
    lo que el panel tiene que mostrar.

    Guarda lo mínimo para eso y nada más: ni la imagen, ni el texto completo. El
    resumen va recortado (`RESUMEN_MAXIMO`) porque su función es reconocer de qué
    escaneo se trata, no reconstruirlo.
    """

    __tablename__ = "ocr_scan_audit"

    id = Column(Integer, primary_key=True, index=True)

    job_id = Column(String, unique=True, nullable=True, index=True)

    user_id = Column(String, index=True, nullable=True)      # auth_users.id, si lo hubo
    user_email = Column(String, index=True, nullable=False)
    origen = Column(String, nullable=False)                  # "app" | "panel"

    doc_type = Column(String, nullable=False, index=True)
    engine = Column(String, nullable=True)                   # motor que ganó la lectura
    status = Column(String, nullable=False)                  # "done" | "failed"

    confianza = Column(Float, nullable=True)                 # 0..1, del documento
    processing_ms = Column(Integer, nullable=True)
    campos = Column(Integer, nullable=True)                  # campos o renglones leídos
    resumen = Column(String, nullable=True)                  # recorte de lo leído, o el error

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
