"""
Registro de auditoría de las ejecuciones del módulo de OCR.

La foto no se guarda: el coordinador la sube como temporal y el worker la
descarta al terminar el job. Eso es deliberado —una imagen de la despensa o de
una factura de alguien no tiene por qué quedar en ningún disco— pero deja al
módulo sin historia, y sin historia no se puede decir si funciona.

Este módulo es el reemplazo: por cada ejecución queda un renglón breve con quién
la pidió, qué tipo de documento era, cuánto tardó, con qué confianza salió y un
recorte de lo que se leyó. Alcanza para auditar el uso y para calcular tiempos y
precisión, y no alcanza para reconstruir la imagen, que es exactamente el punto.
"""
import logging
from typing import Any, Optional

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.audit import OCRScanAudit

logger = logging.getLogger(__name__)

# El resumen sirve para reconocer un escaneo en una lista, no para leerlo de
# nuevo. Un ticket entero son miles de caracteres y no aportan nada acá.
RESUMEN_MAXIMO = 160

ORIGEN_APP = "app"
ORIGEN_PANEL = "panel"


def registrar(
    db: Session,
    *,
    user_email: str,
    origen: str,
    doc_type: str,
    result: Optional[dict[str, Any]] = None,
    job_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: str = "done",
    error: Optional[str] = None,
) -> None:
    """
    Deja constancia de una ejecución. Nunca hace fallar a quien la llama.

    Un problema al auditar no puede tumbar un escaneo que salió bien: el usuario
    perdería el resultado por un renglón de historial. Se registra el error en el
    log del servicio y se sigue.
    """
    if job_id and _ya_registrado(db, job_id):
        return

    result = result or {}

    # El `except` es ancho a propósito: acá se resume una lectura que la armó otro
    # servicio, así que un campo con una forma inesperada no puede costarle el
    # resultado a quien está esperando la foto.
    try:
        db.add(
            OCRScanAudit(
                job_id=job_id,
                user_id=user_id,
                user_email=user_email,
                origen=origen,
                doc_type=doc_type,
                engine=result.get("engine") or None,
                status=status,
                confianza=_confianza(result),
                processing_ms=_entero(result.get("processing_ms")),
                campos=_campos(result),
                resumen=_resumen(result, error, status),
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("No se pudo auditar el escaneo %s: %s", job_id or "(sincrónico)", e)


async def registrar_falla(db: Session, error, **datos) -> None:
    """
    Deja constancia de un escaneo sincrónico que el módulo no pudo resolver.

    Sólo cuenta como ejecución una falla del lado del módulo (5xx: un motor o el
    decodificador caídos). Un 429 por cupo o un 400 por formato son rechazos
    antes de correr nada: auditarlos inflaría las fallidas con cosas que no son
    fallas del OCR.
    """
    if getattr(error, "status_code", 0) < 500:
        return
    await run_in_threadpool(
        registrar, db, status="failed", error=str(getattr(error, "detail", error)), **datos
    )


def _ya_registrado(db: Session, job_id: str) -> bool:
    """
    Un job encolado se consulta muchas veces y termina una sola.

    Sin este corte, la pantalla que hace polling cada medio segundo dejaría un
    renglón de auditoría por consulta desde que el job termina hasta que alguien
    cierra la ventana.
    """
    try:
        return db.query(OCRScanAudit.id).filter(OCRScanAudit.job_id == job_id).first() is not None
    except SQLAlchemyError:
        return False


def _confianza(result: dict[str, Any]) -> Optional[float]:
    valor = result.get("document_confidence")
    return float(valor) if isinstance(valor, (int, float)) else None


def _entero(valor: Any) -> Optional[int]:
    return int(valor) if isinstance(valor, (int, float)) else None


def _campos(result: dict[str, Any]) -> Optional[int]:
    """Cuántas cosas se leyeron: renglones en un comprobante, campos en el resto."""
    items = result.get("items")
    if items:
        return len(items)

    fields = result.get("fields")
    if not isinstance(fields, dict):
        return None
    return sum(1 for campo in fields.values() if isinstance(campo, dict) and campo.get("value") is not None)


def _resumen(result: dict[str, Any], error: Optional[str], status: str) -> Optional[str]:
    """Un recorte de lo leído, o el motivo de la falla."""
    texto = error if status == "failed" else result.get("texto_legible")
    if not texto:
        return None

    texto = " ".join(str(texto).split())
    if len(texto) <= RESUMEN_MAXIMO:
        return texto
    return texto[: RESUMEN_MAXIMO - 1].rstrip() + "…"
