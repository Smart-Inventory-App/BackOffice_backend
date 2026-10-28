"""Las alertas de un usuario (o de toda la plataforma) tal como las ve la app.

Las de vencimiento se calculan al leer, con la fecha de hoy (argentina) y la regla vigente; las del modelo predictivo
(stock bajo por cobertura y ritmo lento) las guarda `app_backend` en la tabla `alerts` y acá se leen, y su nivel
también se calcula con la regla de hoy. Así el backoffice y la app muestran siempre lo mismo sin repetir el cálculo
del modelo.
"""
from collections import defaultdict
from datetime import date
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core import alert_rules
from app.core.argentina import ar_today
from app.models.app_models import Alert, Product, UserProfile
from app.schemas.alerts import AlertOut, AlertSummary

LEVEL_ORDER = {"critical": 0, "warning": 1}
STOCK_TYPE = "stock_low_coverage"
EXPIRY_TYPES = ("expiry", "slow_pace")
PREDICTIVE_TYPES = (STOCK_TYPE, "slow_pace")


def _base(product: Product, **extra) -> dict:
    return dict(
        product_id=product.id,
        product_name=product.nombre,
        product_brand=product.marca,
        product_category=product.categoria,
        product_location=product.ubicacion,
        product_stock=product.stock,
        product_expiry=product.vencimiento,
        image_url=product.imagen_url or None,
        user_id=str(product.user_id),
        **extra,
    )


def alerts_for(
    db: Session, user_id: Optional[str] = None, today: Optional[date] = None, product_ids: Optional[list] = None,
) -> list[AlertOut]:
    """Todas las alertas vigentes, de un usuario o de todos (o solo de esos productos), de las críticas a las advertencias."""
    today = today or ar_today()
    products_query = db.query(Product)
    profiles_query = db.query(UserProfile)
    if user_id is not None:
        products_query = products_query.filter(Product.user_id == user_id)
        profiles_query = profiles_query.filter(UserProfile.user_id == user_id)
    if product_ids is not None:
        products_query = products_query.filter(Product.id.in_(product_ids))
    products = {p.id: p for p in products_query.all()}
    profiles = {p.user_id: p for p in profiles_query.all()}

    stored: dict[int, list[Alert]] = defaultdict(list)
    if products:
        for alert in db.query(Alert).filter(Alert.product_id.in_(list(products))).all():
            stored[alert.product_id].append(alert)

    alerts: list[AlertOut] = []
    for product in products.values():
        profile = profiles.get(str(product.user_id))
        if product.stock > 0 and product.vencimiento is not None:
            limit, scope = alert_rules.expiry_limit(product.dias_alerta_vencimiento, profile)
            days = alert_rules.days_until(product.vencimiento, today)
            level = alert_rules.expiry_level(days, limit)
            if level:
                alerts.append(AlertOut(
                    key=f"expiry-{product.id}", type="expiry", level=level, origin="rule",
                    days_to_expiry=days, limit_days=limit, rule_scope=scope, **_base(product),
                ))
        for row in stored.get(product.id, []):
            if row.type == STOCK_TYPE:
                limit, scope = alert_rules.coverage_limit(product.stock_minimo, profile)
                level = alert_rules.coverage_level(row.days_until_stockout, limit)
                if level is None or product.stock <= 0:
                    continue
                alerts.append(AlertOut(
                    key=f"{STOCK_TYPE}-{row.id}", type=STOCK_TYPE, level=level, origin="rule", predictive=True,
                    days_until_stockout=row.days_until_stockout, suggested_qty=row.suggested_qty,
                    limit_days=limit, rule_scope=scope, **_base(product),
                ))
            elif row.type == "slow_pace" and product.vencimiento is not None and row.units_wasted:
                limit, scope = alert_rules.expiry_limit(product.dias_alerta_vencimiento, profile)
                days = alert_rules.days_until(product.vencimiento, today)
                alerts.append(AlertOut(
                    key=f"slow_pace-{row.id}", type="slow_pace", origin="system", predictive=True,
                    level="critical" if days < limit else "warning", days_to_expiry=days,
                    units_wasted=row.units_wasted, limit_days=limit, rule_scope=scope, **_base(product),
                ))

    # Primero las de las reglas del usuario; dentro de cada grupo, las críticas antes que las advertencias
    alerts.sort(key=lambda a: (a.origin != "rule", LEVEL_ORDER.get(a.level, 2), a.product_name.lower(), a.key))
    return alerts


def matches_type(alert: AlertOut, kind: Optional[str]) -> bool:
    if kind == "stock":
        return alert.type == STOCK_TYPE
    if kind == "vencimiento":
        return alert.type in EXPIRY_TYPES
    return True


def summarize(alerts: Iterable[AlertOut]) -> AlertSummary:
    alerts = list(alerts)
    return AlertSummary(
        critical=sum(1 for a in alerts if a.level == "critical"),
        warning=sum(1 for a in alerts if a.level == "warning"),
        total=len(alerts),
        expiring=sum(1 for a in alerts if a.type == "expiry" and a.level == "critical"),
        low_stock=sum(1 for a in alerts if a.type == STOCK_TYPE and a.level == "critical"),
    )


def levels_by_product(db: Session, product_ids: list) -> dict[int, tuple[str, list[str]]]:
    """Para cada producto con alertas: el nivel más grave y los tipos de alerta que tiene."""
    if not product_ids:
        return {}
    found: dict[int, tuple[str, list[str]]] = {}
    for alert in alerts_for(db, product_ids=product_ids):  # vienen de las más graves a las menos graves
        level, types = found.get(alert.product_id, (alert.level, []))
        if LEVEL_ORDER.get(alert.level, 2) < LEVEL_ORDER.get(level, 2):
            level = alert.level
        if alert.type not in types:
            types.append(alert.type)
        found[alert.product_id] = (level, types)
    return found

