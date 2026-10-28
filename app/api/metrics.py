"""Tendencias de consumo: del usuario (`/me/metrics`) y de toda la plataforma (`/admin/metrics`)."""
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.argentina import AR_TZ, ar_day, ar_today
from app.core.database import get_db
from app.core.security import get_current_admin, get_current_user
from app.models.admin import AdminUser
from app.models.app_models import CONSUMPTION_EVENT_TYPES, AuthUser, ConsumptionEvent, Product
from app.schemas.platform import ConsumoPorDia, MyMetrics, ProductoConsumo

router = APIRouter(tags=["metrics"])

# Cuántos productos trae el ranking de los más consumidos
TOP_PRODUCTS = 5


def _ingresos(db: Session, since_start: datetime, user_id: Optional[str]) -> list[tuple[datetime, int]]:
    """Cada carga de stock desde `since_start` (UTC sin zona): las reposiciones de productos que ya existían y el stock con
    el que se cargó cada producto nuevo.

    El stock inicial de un producto no queda como un movimiento: se reconstruye hacia atrás desde el stock actual,
    deshaciendo todos sus movimientos (así se muestra también en «Últimos cambios»). Las correcciones del historial no
    cuentan: queda el movimiento corregido.
    """
    reversed_ids = {row[0] for row in db.query(ConsumptionEvent.reverses_event_id).filter(ConsumptionEvent.reverses_event_id.isnot(None)).all()}
    restocked = db.query(ConsumptionEvent).filter(
        ConsumptionEvent.event_type == "restocked",
        ConsumptionEvent.consumed_at >= since_start,
        ConsumptionEvent.reverses_event_id.is_(None),
    )
    if user_id is not None:
        restocked = restocked.filter(ConsumptionEvent.user_id == user_id)
    entries = [(e.consumed_at, e.quantity) for e in restocked.all() if e.id not in reversed_ids]

    created = db.query(Product).filter(Product.created_at.isnot(None))
    if user_id is not None:
        created = created.filter(Product.user_id == user_id)
    for product in created.all():
        created_at = product.created_at if product.created_at.tzinfo is None else product.created_at.astimezone(timezone.utc).replace(tzinfo=None)
        if created_at < since_start:
            continue
        effect = sum(
            e.quantity if e.event_type == "restocked" else -e.quantity
            for e in db.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
        )
        initial = product.stock - effect
        if initial > 0:
            entries.append((product.created_at, initial))
    return entries


def build_metrics(db: Session, period: int, user_id: Optional[str] = None) -> MyMetrics:
    """Consumo de los últimos `period` días (contando hoy), con los días y el «hoy» de Argentina.

    Con `user_id` es el de ese usuario; sin él, el de toda la plataforma. Las correcciones del historial no cuentan: queda
    el movimiento corregido. El ranking agrupa los lotes de un mismo producto (mismo nombre) para no repetirlo.
    """
    today = ar_today()
    since = today - timedelta(days=period - 1)
    since_start = datetime.combine(since, time.min, tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    ingresos = _ingresos(db, since_start, user_id)

    reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(
        ConsumptionEvent.reverses_event_id.isnot(None)
    )
    query = db.query(ConsumptionEvent).filter(
        ConsumptionEvent.consumed_at >= since_start,
        ConsumptionEvent.reverses_event_id.is_(None),
        ConsumptionEvent.id.notin_(reversed_ids),
    )
    if user_id is not None:
        query = query.filter(ConsumptionEvent.user_id == user_id)
    events = query.all()

    total_consumido = sum(e.quantity for e in events if e.event_type in CONSUMPTION_EVENT_TYPES)
    total_entradas = sum(e.quantity for e in events if e.event_type == "restocked")

    per_day: dict[str, int] = {}
    per_product: dict[int, int] = {}
    for e in events:
        if e.event_type not in CONSUMPTION_EVENT_TYPES:
            continue
        day = ar_day(e.consumed_at).isoformat()
        per_day[day] = per_day.get(day, 0) + e.quantity
        per_product[e.product_id] = per_product.get(e.product_id, 0) + e.quantity

    consumo_por_dia = []
    for i in range(period - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        consumo_por_dia.append(ConsumoPorDia(day=d, quantity=per_day.get(d, 0)))

    names = {}
    if per_product:
        names = {
            p.id: p.nombre
            for p in db.query(Product.id, Product.nombre).filter(Product.id.in_(list(per_product))).all()
        }

    per_day_in: dict[str, int] = {}
    for moment, quantity in ingresos:
        day = ar_day(moment).isoformat()
        per_day_in[day] = per_day_in.get(day, 0) + quantity
    ingresos_por_dia = []
    for i in range(period - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        ingresos_por_dia.append(ConsumoPorDia(day=d, quantity=per_day_in.get(d, 0)))

    grouped: dict[str, dict] = {}
    for pid, qty in per_product.items():
        name = names.get(pid, f"Producto #{pid}")
        entry = grouped.setdefault(" ".join(name.lower().split()), {"product_id": pid, "nombre": name, "best": qty, "cantidad": 0})
        entry["cantidad"] += qty
        if qty > entry["best"]:
            entry.update(product_id=pid, nombre=name, best=qty)

    ranked = sorted(
        (ProductoConsumo(product_id=e["product_id"], nombre=e["nombre"], cantidad=e["cantidad"]) for e in grouped.values()),
        key=lambda p: (-p.cantidad, p.nombre.lower()),
    )

    return MyMetrics(
        period_days=period,
        total_consumido=total_consumido,
        total_entradas=total_entradas,
        consumo_por_dia=consumo_por_dia,
        top_productos=ranked[:TOP_PRODUCTS],
        total_ingresos=sum(per_day_in.values()),
        ingresos_por_dia=ingresos_por_dia,
    )


@router.get("/me/metrics", response_model=MyMetrics)
def my_metrics(
    period: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    return build_metrics(db, period, str(current.id))


@router.get("/admin/metrics", response_model=MyMetrics)
def platform_metrics(
    period: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    return build_metrics(db, period)
