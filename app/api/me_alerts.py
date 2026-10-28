"""Centro de alertas del usuario: las mismas reglas y niveles que la app."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core import alerts_view
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.app_models import AuthUser
from app.schemas.alerts import AlertList, AlertSummary

router = APIRouter(prefix="/me/alerts", tags=["me"])


@router.get("/", response_model=AlertList)
def my_alerts(
    tipo: Optional[str] = Query(None, pattern="^(stock|vencimiento)$"),
    level: Optional[str] = Query(None, pattern="^(critical|warning)$"),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Las alertas del usuario. El resumen cuenta lo del tipo pedido (no lo del nivel), así los números de «Críticas» y
    «Advertencias» siempre coinciden con lo que se ve al tocarlos."""
    of_type = [a for a in alerts_view.alerts_for(db, str(current.id)) if alerts_view.matches_type(a, tipo)]
    items = [a for a in of_type if level is None or a.level == level]
    return AlertList(items=items, summary=alerts_view.summarize(of_type))


@router.get("/summary", response_model=AlertSummary)
def my_alert_summary(db: Session = Depends(get_db), current: AuthUser = Depends(get_current_user)):
    return alerts_view.summarize(alerts_view.alerts_for(db, str(current.id)))
