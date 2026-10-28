from datetime import date
from typing import List, Optional

from pydantic import BaseModel


class AlertOut(BaseModel):
    """Una alerta de un producto, con lo necesario para mostrarla y explicarla."""

    key: str
    product_id: int
    type: str  # expiry | stock_low_coverage | slow_pace
    level: str  # critical | warning
    # «rule»: sale de una regla del usuario (vencimiento, stock mínimo); «system»: la genera el sistema por el consumo
    origin: str
    days_to_expiry: Optional[int] = None
    days_until_stockout: Optional[int] = None
    units_wasted: Optional[int] = None
    suggested_qty: Optional[int] = None
    limit_days: Optional[int] = None
    rule_scope: Optional[str] = None
    # Las del modelo predictivo llevan la marca «Predicción»
    predictive: bool = False
    product_name: str
    product_brand: Optional[str] = None
    product_category: Optional[str] = None
    product_location: Optional[str] = None
    product_stock: int
    product_expiry: Optional[date] = None
    image_url: Optional[str] = None
    user_id: Optional[str] = None


class AlertSummary(BaseModel):
    critical: int
    warning: int
    total: int
    # Productos con alertas de la opción «Vencimiento» (vencimiento y ritmo lento) y de la opción «Stock», de cualquier nivel:
    # los mismos números que muestra el inicio de la app
    expiring: int
    low_stock: int


class AlertList(BaseModel):
    items: List[AlertOut]
    summary: AlertSummary
