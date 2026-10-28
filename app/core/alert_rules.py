"""Reglas de las alertas, las mismas que usa la app: de dónde sale cada límite y cuándo una alerta es crítica o advertencia.

Hay dos reglas que configura el usuario, en su perfil (valen para todos sus productos) y, si quiere, en cada producto
(la del producto manda sobre la del perfil):

- **Vencimiento**: días de anticipación. Se alerta cuando faltan menos de esos días para que venza (o ya venció).
- **Stock mínimo**: días de cobertura. Se alerta cuando, al ritmo de consumo que calcula el modelo predictivo, el stock
  alcanza para menos de esos días.

Para las dos vale el mismo criterio de nivel: crítica si ya se llegó al límite, advertencia si se está cerca (dentro de
un margen que depende del límite) y sin alerta si está lejos.
"""
import math
from datetime import date

NEAR_RATIO = 0.5
MIN_NEAR_DAYS = 2

DEFAULT_COVERAGE_DAYS = 7
DEFAULT_EXPIRY_DAYS = 7

# Rango que acepta el perfil, como en la app
MIN_RULE_DAYS = 2
MAX_RULE_DAYS = 90

SCOPE_PRODUCT = "product"
SCOPE_GENERAL = "general"


def near_margin(limit: int) -> int:
    """Cuántos días más allá del límite todavía se considera «cerca»."""
    return max(MIN_NEAR_DAYS, math.ceil(limit * NEAR_RATIO))


def resolve_limit(own: int | None, general: int | None, default: int) -> tuple[int, str]:
    """El límite que rige y de dónde sale: la regla propia del producto, la general del perfil o la de fábrica."""
    if own:
        return own, SCOPE_PRODUCT
    if general:
        return general, SCOPE_GENERAL
    return default, SCOPE_GENERAL


def expiry_limit(product_days: int | None, profile) -> tuple[int, str]:
    return resolve_limit(product_days, getattr(profile, "dias_alerta_vencimiento", None), DEFAULT_EXPIRY_DAYS)


def coverage_limit(product_days: int | None, profile) -> tuple[int, str]:
    return resolve_limit(product_days, getattr(profile, "stock_minimo", None), DEFAULT_COVERAGE_DAYS)


def expiry_level(days_to_expiry: int, limit: int) -> str | None:
    if days_to_expiry < limit:
        return "critical"
    if days_to_expiry < limit + near_margin(limit):
        return "warning"
    return None


def coverage_level(days_until_stockout: int | None, limit: int) -> str | None:
    if days_until_stockout is None:
        return None
    if days_until_stockout < limit:
        return "critical"
    if days_until_stockout < limit + near_margin(limit):
        return "warning"
    return None


def days_until(expiry: date, today: date) -> int:
    return (expiry - today).days
