"""Avisos al `app_backend`, que es quien calcula las alertas del modelo predictivo."""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def refresh_user_alerts(user_id: str) -> bool:
    """Le pide al `app_backend` que vuelva a evaluar las alertas del usuario, sin mandarle avisos.

    Se usa al cambiar una regla de alertas: con otro límite, las alertas de stock pueden ser otras. Es de mejor
    esfuerzo: si el servicio no responde, el cambio ya quedó guardado y las alertas se corrigen en el próximo chequeo.
    """
    try:
        response = httpx.post(
            f"{settings.APP_BACKEND_URL}/internal/alerts/run/{user_id}",
            params={"notify": "false"},
            headers={"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN},
            timeout=60.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.warning("No se pudieron actualizar las alertas del usuario %s", user_id, exc_info=True)
        return False
