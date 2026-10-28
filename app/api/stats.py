from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import alerts_view
from app.core.argentina import AR_TZ, ar_today
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import CONSUMPTION_EVENT_TYPES, AuthUser, ConsumptionEvent, Product
from app.schemas.platform import (
    CategoryCount,
    DashboardStats,
    DayCount,
    UserProductCount,
)

router = APIRouter(prefix="/admin/stats", tags=["admin-stats"])


@router.get("/", response_model=DashboardStats)
def dashboard(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    today = ar_today()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    total_users = db.query(func.count(AuthUser.id)).scalar() or 0
    active_users = (
        db.query(func.count(AuthUser.id))
        .filter(AuthUser.is_active.is_(True))
        .scalar()
        or 0
    )
    new_users_last_7d = (
        db.query(func.count(AuthUser.id))
        .filter(AuthUser.created_at >= now - timedelta(days=7))
        .scalar()
        or 0
    )

    total_products = db.query(func.count(Product.id)).scalar() or 0
    products_with_image = (
        db.query(func.count(Product.id))
        .filter(Product.imagen_url.isnot(None), Product.imagen_url != "")
        .scalar()
        or 0
    )
    total_units = db.query(func.coalesce(func.sum(Product.stock), 0)).scalar() or 0
    expired = (
        db.query(func.count(Product.id)).filter(Product.vencimiento < today, Product.stock > 0).scalar() or 0
    )

    # Alertas de toda la plataforma, con las reglas de cada usuario y el mismo criterio que la app
    alerts = alerts_view.alerts_for(db, today=today)
    summary = alerts_view.summarize(alerts)
    users_with_alerts = len({a.user_id for a in alerts})

    # Consumo y reposición de los últimos 7 días contando hoy (días argentinos), sin las correcciones del historial
    week_start = datetime.combine(today - timedelta(days=6), time.min, tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(ConsumptionEvent.reverses_event_id.isnot(None))
    week_events = (
        db.query(ConsumptionEvent.event_type, func.coalesce(func.sum(ConsumptionEvent.quantity), 0))
        .filter(
            ConsumptionEvent.consumed_at >= week_start,
            ConsumptionEvent.reverses_event_id.is_(None),
            ConsumptionEvent.id.notin_(reversed_ids),
        )
        .group_by(ConsumptionEvent.event_type)
        .all()
    )
    consumed_7d = sum(int(qty) for kind, qty in week_events if kind in CONSUMPTION_EVENT_TYPES)
    restocked_7d = sum(int(qty) for kind, qty in week_events if kind == "restocked")

    by_category = [
        CategoryCount(label=row[0] or "Sin categoría", count=row[1])
        for row in db.query(Product.categoria, func.count(Product.id))
        .group_by(Product.categoria)
        .order_by(func.count(Product.id).desc())
        .all()
    ]

    email_map = {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}
    top_rows = (
        db.query(Product.user_id, func.count(Product.id))
        .group_by(Product.user_id)
        .order_by(func.count(Product.id).desc())
        .limit(5)
        .all()
    )
    top_users = [
        UserProductCount(user_id=uid, email=email_map.get(uid), count=count)
        for uid, count in top_rows
    ]

    # Products created per day over the last 14 days (zero-filled)
    raw = (
        db.query(func.date(Product.created_at), func.count(Product.id))
        .filter(Product.created_at >= now - timedelta(days=14))
        .group_by(func.date(Product.created_at))
        .all()
    )
    per_day_map = {str(d): c for d, c in raw}
    per_day = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        per_day.append(DayCount(day=d.isoformat(), count=per_day_map.get(d.isoformat(), 0)))

    return DashboardStats(
        total_users=total_users,
        active_users=active_users,
        inactive_users=total_users - active_users,
        new_users_last_7d=new_users_last_7d,
        total_products=total_products,
        products_with_image=products_with_image,
        low_stock_products=summary.low_stock,
        expiring_soon_products=summary.expiring,
        expired_products=expired,
        total_units=int(total_units),
        critical_alerts=summary.critical,
        warning_alerts=summary.warning,
        users_with_alerts=users_with_alerts,
        consumed_last_7d=consumed_7d,
        restocked_last_7d=restocked_7d,
        products_by_category=by_category,
        top_users_by_products=top_users,
        products_per_day_last_14d=per_day,
    )
