from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit, UserStatusAudit
from app.schemas.platform import ActivityEntry, PaginatedActivity

router = APIRouter(prefix="/admin/activity", tags=["admin-activity"])

ACTIVITY_KINDS = "^(status|role|login|registered|product_created|product_updated|product_deleted)$"


def _sync_registrations(db: Session) -> None:
    """Detecta usuarios de `auth_users` que todavía no tienen un evento
    'registered' en el log y los agrega, usando la fecha real de alta (no la
    fecha en que corre esta sincronización). No hay forma de engancharse al
    momento exacto del registro porque lo maneja un servicio de auth externo,
    así que esto se resuelve con esta sincronización perezosa."""
    logged_ids = {
        row[0]
        for row in db.query(AdminActivityLog.target_user_id)
        .filter(AdminActivityLog.event_type == "registered")
        .all()
    }
    missing = [u for u in db.query(AuthUser).all() if str(u.id) not in logged_ids]
    for u in missing:
        db.add(
            AdminActivityLog(
                event_type="registered",
                actor_email=None,
                target_user_id=str(u.id),
                target_email=u.email,
                description=f"{u.email} se registró en la app",
                created_at=u.created_at,
            )
        )
    if missing:
        db.commit()


@router.get("/", response_model=PaginatedActivity)
def list_activity(
    kind: Optional[str] = Query(None, pattern=ACTIVITY_KINDS),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    _sync_registrations(db)
    emails = {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}
    entries: list[ActivityEntry] = []

    if kind in (None, "status"):
        for e in db.query(UserStatusAudit).all():
            target_email = emails.get(e.user_id)
            desc = f"{target_email or e.user_id} fue {'habilitado' if e.is_active else 'deshabilitado'}"
            if e.reason:
                desc += f" ({e.reason})"
            entries.append(
                ActivityEntry(
                    id=f"status-{e.id}",
                    kind="status",
                    actor_email=e.changed_by,
                    target_user_id=e.user_id,
                    target_email=target_email,
                    description=desc,
                    created_at=e.created_at,
                )
            )

    if kind in (None, "role"):
        for e in db.query(AdminRoleAudit).all():
            target_email = emails.get(e.user_id)
            verb = "recibió" if e.action == "granted" else "perdió"
            entries.append(
                ActivityEntry(
                    id=f"role-{e.id}",
                    kind="role",
                    actor_email=e.changed_by,
                    target_user_id=e.user_id,
                    target_email=target_email,
                    description=f"{target_email or e.user_id} {verb} el rol de administrador",
                    created_at=e.created_at,
                )
            )

    activity_log_kinds = ("login", "registered", "product_created", "product_updated", "product_deleted")
    if kind in (None,) + activity_log_kinds:
        query = db.query(AdminActivityLog)
        if kind:
            query = query.filter(AdminActivityLog.event_type == kind)
        for e in query.all():
            entries.append(
                ActivityEntry(
                    id=f"activity-{e.id}",
                    kind=e.event_type,
                    actor_email=e.actor_email,
                    target_user_id=e.target_user_id,
                    target_email=e.target_email,
                    description=e.description,
                    created_at=e.created_at,
                )
            )

    entries.sort(key=lambda e: e.created_at or datetime.min, reverse=True)
    total = len(entries)
    return PaginatedActivity(items=entries[offset : offset + limit], total=total)
