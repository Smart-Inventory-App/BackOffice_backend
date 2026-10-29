from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.ocr import scan_stats
from app.core.argentina import AR_TZ, ar_day, ar_today
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, ConsumptionEvent, Product, UserProfile
from app.models.audit import AdminActivityLog
from app.schemas.platform import (
    ActiveUserRow,
    ActivityDay,
    DayCount,
    InventoryOverview,
    OcrOverview,
    PlatformOverview,
    UsersOverview,
)

router = APIRouter(prefix="/admin/stats", tags=["admin-stats"])

# Cuántos usuarios trae el ranking de los más activos
TOP_ACTIVE_USERS = 10


def _since_start(today, days: int) -> datetime:
    """El comienzo (en UTC, sin zona) del primer día de un período de `days` días argentinos que termina hoy."""
    first = today - timedelta(days=days - 1)
    return datetime.combine(first, time.min, tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def _per_day(today, days: int, counts: dict[str, int]) -> list[DayCount]:
    return [
        DayCount(day=(today - timedelta(days=i)).isoformat(), count=counts.get((today - timedelta(days=i)).isoformat(), 0))
        for i in range(days - 1, -1, -1)
    ]


@router.get("/overview", response_model=PlatformOverview)
def overview(
    period: int = Query(14, ge=1, le=365),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """Lo que muestra el dashboard del administrador para analizar cómo funciona y cómo rinde la plataforma.

    Reúne usuarios (altas, adopción y actividad), movimientos de stock por día, los usuarios más activos, el inventario
    que alimenta la calidad de datos y el rendimiento del módulo de OCR. Los días son los de Argentina y el período
    termina hoy.
    """
    today = ar_today()
    start = _since_start(today, period)
    inactive_ids = {str(row[0]) for row in db.query(AuthUser.id).filter(AuthUser.is_active.is_(False)).all()}
    emails = {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}

    # ── usuarios
    all_users = db.query(AuthUser.created_at, AuthUser.is_active).all()
    registered: dict[str, int] = {}
    new_in_period = 0
    for created_at, _active in all_users:
        if created_at is None:
            continue
        naive = created_at.astimezone(timezone.utc).replace(tzinfo=None) if created_at.tzinfo else created_at
        if naive >= start:
            new_in_period += 1
            day = ar_day(created_at).isoformat()
            registered[day] = registered.get(day, 0) + 1
    active_users = sum(1 for _created, is_active in all_users if is_active)

    # ── movimientos de stock del período (sin las correcciones del historial)
    reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(ConsumptionEvent.reverses_event_id.isnot(None))
    events = (
        db.query(ConsumptionEvent)
        .filter(
            ConsumptionEvent.consumed_at >= start,
            ConsumptionEvent.reverses_event_id.is_(None),
            ConsumptionEvent.id.notin_(reversed_ids),
        )
        .all()
    )
    logins = (
        db.query(AdminActivityLog)
        .filter(AdminActivityLog.event_type == "login", AdminActivityLog.created_at >= start)
        .all()
    )
    activity: dict[str, ActivityDay] = {
        (today - timedelta(days=i)).isoformat(): ActivityDay(day=(today - timedelta(days=i)).isoformat())
        for i in range(period - 1, -1, -1)
    }
    movements_by_user: dict[str, int] = {}
    entries_by_user: dict[str, int] = {}
    logged_in: set[str] = set()
    for e in events:
        day = ar_day(e.consumed_at).isoformat()
        row = activity.get(day)
        if row is not None:
            if e.event_type == "restocked":
                row.entradas += 1
            else:
                row.salidas += 1
        movements_by_user[str(e.user_id)] = movements_by_user.get(str(e.user_id), 0) + 1
        if e.event_type == "restocked":
            entries_by_user[str(e.user_id)] = entries_by_user.get(str(e.user_id), 0) + 1
    for entry in logins:
        row = activity.get(ar_day(entry.created_at).isoformat())
        if row is not None:
            row.logins += 1
        if entry.target_user_id:
            logged_in.add(entry.target_user_id)
    active_in_period = len(set(movements_by_user) | logged_in)

    # ── inventario
    products = db.query(Product).all()
    products_by_user: dict[str, int] = {}
    units_by_user: dict[str, int] = {}
    with_image = without_expiry = units = 0
    for p in products:
        uid = str(p.user_id)
        products_by_user[uid] = products_by_user.get(uid, 0) + 1
        units_by_user[uid] = units_by_user.get(uid, 0) + p.stock
        units += p.stock
        with_image += 1 if p.imagen_url else 0
        without_expiry += 1 if p.vencimiento is None else 0

    # ── usuarios más activos: por movimientos de stock en el período, con el nombre y el usuario de su perfil
    profiles = {p.user_id: p for p in db.query(UserProfile).all()}
    ranking = sorted(movements_by_user, key=lambda uid: (-movements_by_user[uid], emails.get(uid) or uid))[:TOP_ACTIVE_USERS]
    most_active = [
        ActiveUserRow(
            user_id=uid,
            email=emails.get(uid),
            name=" ".join(filter(None, [profiles[uid].first_name, profiles[uid].last_name])) or None if uid in profiles else None,
            username=profiles[uid].username if uid in profiles else None,
            movements=movements_by_user[uid],
            entries=entries_by_user.get(uid, 0),
            exits=movements_by_user[uid] - entries_by_user.get(uid, 0),
            products=products_by_user.get(uid, 0),
            units=int(units_by_user.get(uid, 0)),
            is_active=uid not in inactive_ids,
        )
        for uid in ranking
    ]

    # ── OCR (su propio historial)
    ocr = scan_stats(dias=period, db=db, _=None)

    return PlatformOverview(
        period_days=period,
        users=UsersOverview(
            total=len(all_users),
            active=active_users,
            new_in_period=new_in_period,
            active_in_period=active_in_period,
            with_products=len({uid for uid in products_by_user if uid not in inactive_ids}),
            registrations_per_day=_per_day(today, period, registered),
        ),
        activity_per_day=list(activity.values()),
        inventory=InventoryOverview(
            products=len(products),
            units=int(units),
            with_image=with_image,
            without_expiry=without_expiry,
        ),
        most_active_users=most_active,
        ocr=OcrOverview(
            runs=ocr.ejecuciones,
            succeeded=ocr.exitosas,
            avg_ms=ocr.ms_promedio,
            avg_confidence=ocr.confianza_promedio,
            by_type=[t.model_dump() for t in ocr.por_tipo],
        ),
    )
