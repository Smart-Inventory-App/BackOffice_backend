from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.app_models import CONSUMPTION_EVENT_TYPES, AuthUser, ConsumptionEvent, Product
from app.schemas.platform import ConsumoPorDia, MyMetrics, ProductoConsumo

router = APIRouter(prefix="/me", tags=["me"])

# Hora argentina (UTC-3, sin horario de verano): los días del gráfico se cuentan con ella. Los eventos se guardan en UTC.
AR_TZ = timezone(timedelta(hours=-3))


def _ar_day(moment: datetime):
    """Día calendario argentino de un instante. Sin zona horaria se asume UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(AR_TZ).date()


@router.get("/metrics", response_model=MyMetrics)
def my_metrics(
    period: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    today = datetime.now(AR_TZ).date()
    since = today - timedelta(days=period - 1)
    since_start = datetime.combine(since, time.min, tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)

    reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(
        ConsumptionEvent.reverses_event_id.isnot(None)
    )
    base = (
        db.query(ConsumptionEvent)
        .filter(
            ConsumptionEvent.user_id == uid,
            ConsumptionEvent.consumed_at >= since_start,
            ConsumptionEvent.reverses_event_id.is_(None),
            ConsumptionEvent.id.notin_(reversed_ids),
        )
    )
    events = base.all()

    total_consumido = sum(e.quantity for e in events if e.event_type in CONSUMPTION_EVENT_TYPES)
    total_entradas = sum(e.quantity for e in events if e.event_type == "restocked")

    per_day: dict[str, int] = {}
    for e in events:
        if e.event_type not in CONSUMPTION_EVENT_TYPES:
            continue
        day = _ar_day(e.consumed_at).isoformat()
        per_day[day] = per_day.get(day, 0) + e.quantity

    consumo_por_dia = []
    for i in range(period - 1, -1, -1):
        d = today - timedelta(days=i)
        consumo_por_dia.append(ConsumoPorDia(day=d.isoformat(), quantity=per_day.get(d.isoformat(), 0)))

    per_product: dict[int, int] = {}
    for e in events:
        if e.event_type not in CONSUMPTION_EVENT_TYPES:
            continue
        per_product[e.product_id] = per_product.get(e.product_id, 0) + e.quantity

    product_ids = list(per_product.keys())
    names = {}
    if product_ids:
        names = {
            p.id: p.nombre
            for p in db.query(Product.id, Product.nombre).filter(Product.id.in_(product_ids)).all()
        }

    ranked = sorted(
        (
            ProductoConsumo(product_id=pid, nombre=names.get(pid, f"Producto #{pid}"), cantidad=qty)
            for pid, qty in per_product.items()
        ),
        key=lambda p: p.cantidad,
        reverse=True,
    )

    return MyMetrics(
        period_days=period,
        total_consumido=total_consumido,
        total_entradas=total_entradas,
        consumo_por_dia=consumo_por_dia,
        top_productos=ranked[:5],
        productos_menos_usados=list(reversed(ranked[-5:])) if len(ranked) > 5 else [],
    )
