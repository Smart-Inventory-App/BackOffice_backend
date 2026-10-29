import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, ConsumptionEvent, Product
from app.models.audit import AdminActivityLog, AdminRoleAudit, UserStatusAudit
from app.schemas.platform import ActivityEntry, PaginatedActivity

router = APIRouter(prefix="/admin/activity", tags=["admin-activity"])

ACTIVITY_KINDS = "^(status|role|login|registered|product_created|product_exit|product_updated|product_deleted)$"

# Las cargas de stock a un producto que ya existía ("ingresó N unidades a…", "sumó N unidades a…") siempre fueron
# entradas; las que se guardaron antes como ediciones se reconocen por su texto
_LOADED_STOCK = re.compile(r" (ingresó|sumó) \d+ unidad(?:es)? a ")

EXIT_REASONS = {"consumed": "consumido", "sold": "vendido", "rented": "alquilado", "expired": "vencido"}


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


@router.get("/filters/options")
def filter_options(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """Los usuarios por los que se puede filtrar la auditoría, ordenados por mail."""
    users = sorted(
        ({"user_id": str(u.id), "email": u.email} for u in db.query(AuthUser.id, AuthUser.email).all()),
        key=lambda u: (u["email"] or u["user_id"]).lower(),
    )
    return {"users": users}


@router.get("/", response_model=PaginatedActivity)
def list_activity(
    kind: Optional[str] = Query(None, pattern=ACTIVITY_KINDS),
    user_id: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    _sync_registrations(db)
    emails = {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}
    entries: list[ActivityEntry] = []

    if kind in (None, "status"):
        status_query = db.query(UserStatusAudit)
        if user_id:
            status_query = status_query.filter(UserStatusAudit.user_id == user_id)
        for e in status_query.all():
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
        role_query = db.query(AdminRoleAudit)
        if user_id:
            role_query = role_query.filter(AdminRoleAudit.user_id == user_id)
        for e in role_query.all():
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

    if kind in (None, "product_exit"):
        # Cada salida de stock de cada usuario sale del registro de consumo (app y dashboard, también las anteriores).
        # Las correcciones del historial no cuentan: queda el movimiento corregido.
        reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(ConsumptionEvent.reverses_event_id.isnot(None))
        exits = (
            db.query(ConsumptionEvent)
            .filter(
                ConsumptionEvent.event_type != "restocked",
                ConsumptionEvent.reverses_event_id.is_(None),
                ConsumptionEvent.id.notin_(reversed_ids),
                *([ConsumptionEvent.user_id == user_id] if user_id else []),
            )
            .all()
        )
        product_names = {p.id: p.nombre for p in db.query(Product.id, Product.nombre).all()}
        for e in exits:
            owner_email = emails.get(str(e.user_id))
            who = owner_email or e.user_id
            name = product_names.get(e.product_id, f"producto #{e.product_id}")
            reason = EXIT_REASONS.get(e.event_type, "consumido")
            entries.append(
                ActivityEntry(
                    id=f"exit-{e.id}",
                    kind="product_exit",
                    actor_email=e.actor_email,
                    target_user_id=str(e.user_id),
                    target_email=owner_email,
                    description=f"{who} sacó {e.quantity} {'unidad' if e.quantity == 1 else 'unidades'} de \"{name}\" ({reason})",
                    created_at=e.consumed_at,
                )
            )

    activity_log_kinds = ("login", "registered", "product_created", "product_updated", "product_deleted")
    if kind in (None,) + activity_log_kinds:
        query = db.query(AdminActivityLog)
        if kind in ("product_created", "product_updated"):
            query = query.filter(AdminActivityLog.event_type.in_(("product_created", "product_updated")))
        elif kind:
            query = query.filter(AdminActivityLog.event_type == kind)
        if user_id:
            query = query.filter(AdminActivityLog.target_user_id == user_id)
        for e in query.all():
            event_kind = "product_created" if e.event_type == "product_updated" and _LOADED_STOCK.search(e.description) else e.event_type
            if kind and event_kind != kind:
                continue
            entries.append(
                ActivityEntry(
                    id=f"activity-{e.id}",
                    kind=event_kind,
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
