from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import ocr_audit, ocr_gateway
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.audit import OCRScanAudit

router = APIRouter(prefix="/admin/ocr", tags=["admin-ocr"])


class EngineStatus(BaseModel):
    name: str
    url: str = ""
    available: bool = False
    version: str = ""
    replicas: List[str] = Field(
        default_factory=list,
        description="Instancias del motor. PaddleOCR va replicado; el panel tiene que mostrarlas.",
    )


class OCRStatus(BaseModel):
    available: bool
    checks: dict = Field(
        default_factory=dict,
        description="Redis, motor configurado y decodificador de códigos, según el coordinador.",
    )
    engines: List[EngineStatus] = Field(default_factory=list)


@router.get("/status", response_model=OCRStatus)
async def ocr_status(_: AdminUser = Depends(get_current_admin)):
    """
    Estado del módulo de OCR para el panel de plataforma.

    No falla si el módulo está caído: eso es justamente lo que el panel tiene que
    poder mostrar, así que devuelve `available: false` con lo que haya podido
    averiguar.
    """
    checks, available = await _readyz()
    return OCRStatus(available=available, checks=checks, engines=await _engines())


@router.post("/scan")
async def scan(
    file: UploadFile = File(...),
    doc_type: str = Form(default="product_label"),
    engine: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Procesa una foto y devuelve la lectura completa, sin encolar.

    Es sincrónico y devuelve todo lo que el módulo leyó —confianza por campo,
    texto legible, motor ganador, intentos— porque acá se diagnostica por qué una
    foto sale mal. `engine` permite forzar un motor para compararlos.

    La imagen no se guarda en ningún lado: va directo al coordinador, que la
    procesa en memoria.
    """
    ocr_gateway.validate_doc_type(doc_type)
    content = await ocr_gateway.read_image(file)

    data = {"doc_type": doc_type}
    if engine:
        data["engine"] = engine

    lectura = await ocr_gateway.request(
        "post",
        "/ocr/sync/upload",
        files={"file": (file.filename or "scan.jpg", content, file.content_type)},
        data=data,
    )

    await run_in_threadpool(
        ocr_audit.registrar,
        db,
        user_email=admin.email,
        origen=ocr_audit.ORIGEN_PANEL,
        doc_type=doc_type,
        result=lectura.get("result"),
    )
    return lectura


class ScanAuditEntry(BaseModel):
    """Un renglón del historial. Breve a propósito: la foto no existe más."""

    id: int
    job_id: Optional[str] = None
    user_email: str
    user_id: Optional[str] = None
    origen: str
    doc_type: str
    engine: Optional[str] = None
    status: str
    confianza: Optional[float] = None
    processing_ms: Optional[int] = None
    campos: Optional[int] = None
    resumen: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScanAuditPage(BaseModel):
    items: List[ScanAuditEntry]
    total: int


class DocTypeStats(BaseModel):
    doc_type: str
    ejecuciones: int
    exitosas: int
    fallidas: int
    ms_promedio: Optional[int] = None
    ms_maximo: Optional[int] = None
    confianza_promedio: Optional[float] = None


class OCRStats(BaseModel):
    """Cómo viene funcionando el módulo, según su propio historial."""

    dias: int
    ejecuciones: int
    exitosas: int
    fallidas: int
    usuarios: int
    ms_promedio: Optional[int] = None
    confianza_promedio: Optional[float] = None
    por_tipo: List[DocTypeStats] = Field(default_factory=list)


@router.get("/audit", response_model=ScanAuditPage)
def scan_audit(
    user: Optional[str] = Query(default=None, description="Filtra por email de quien escaneó"),
    doc_type: Optional[str] = Query(default=None),
    origen: Optional[str] = Query(default=None, pattern="^(app|panel)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """
    Historial de ejecuciones del módulo, de la más reciente a la más vieja.

    Es lo único que queda de cada escaneo: las fotos se descartan al terminar el
    procesamiento y no se guardan en ningún lado. Por eso el renglón trae un
    resumen recortado de lo leído y no el texto completo — alcanza para reconocer
    de qué escaneo se trata y para auditar quién usó el módulo, que es para lo
    que está.
    """
    query = db.query(OCRScanAudit)
    if user:
        query = query.filter(OCRScanAudit.user_email.ilike(f"%{user}%"))
    if doc_type:
        query = query.filter(OCRScanAudit.doc_type == doc_type)
    if origen:
        query = query.filter(OCRScanAudit.origen == origen)

    total = query.count()
    items = (
        query.order_by(OCRScanAudit.created_at.desc(), OCRScanAudit.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return ScanAuditPage(items=items, total=total)


@router.get("/stats", response_model=OCRStats)
def scan_stats(
    dias: int = Query(default=7, ge=1, le=365),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """
    Tiempos y precisión medidos sobre las ejecuciones reales, no sobre un banco.

    El promedio de confianza es lo más parecido a una precisión que se puede dar
    sin conocer la respuesta correcta: es lo que el propio módulo dice de sus
    lecturas. Se lo mira por tipo de documento porque mezclarlos no significa
    nada — un código de barras acierta o falla y vuelve en dos décimas, y un
    comprobante tarda un minuto y siempre necesita revisión.
    """
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    base = db.query(OCRScanAudit).filter(OCRScanAudit.created_at >= desde)

    filas = (
        db.query(
            OCRScanAudit.doc_type,
            func.count(OCRScanAudit.id),
            func.count(OCRScanAudit.id).filter(OCRScanAudit.status == "done"),
            func.avg(OCRScanAudit.processing_ms),
            func.max(OCRScanAudit.processing_ms),
            func.avg(OCRScanAudit.confianza),
        )
        .filter(OCRScanAudit.created_at >= desde)
        .group_by(OCRScanAudit.doc_type)
        .order_by(func.count(OCRScanAudit.id).desc())
        .all()
    )

    por_tipo = [
        DocTypeStats(
            doc_type=doc_type,
            ejecuciones=ejecuciones,
            exitosas=exitosas,
            fallidas=ejecuciones - exitosas,
            ms_promedio=_redondear(ms_promedio),
            ms_maximo=_redondear(ms_maximo),
            confianza_promedio=_porcentaje(confianza),
        )
        for doc_type, ejecuciones, exitosas, ms_promedio, ms_maximo, confianza in filas
    ]

    totales = base.with_entities(
        func.count(OCRScanAudit.id),
        func.count(OCRScanAudit.id).filter(OCRScanAudit.status == "done"),
        func.count(func.distinct(OCRScanAudit.user_email)),
        func.avg(OCRScanAudit.processing_ms),
        func.avg(OCRScanAudit.confianza),
    ).one()
    ejecuciones, exitosas, usuarios, ms_promedio, confianza = totales

    return OCRStats(
        dias=dias,
        ejecuciones=ejecuciones,
        exitosas=exitosas,
        fallidas=ejecuciones - exitosas,
        usuarios=usuarios,
        ms_promedio=_redondear(ms_promedio),
        confianza_promedio=_porcentaje(confianza),
        por_tipo=por_tipo,
    )


def _redondear(valor) -> Optional[int]:
    return int(round(float(valor))) if valor is not None else None


def _porcentaje(valor) -> Optional[float]:
    return round(float(valor), 4) if valor is not None else None


@router.post("/temp/purge")
async def purge_temp(_: AdminUser = Depends(get_current_admin)):
    """
    Borra las imágenes temporales de escaneo que quedaron sin descartar.

    El worker del OCR descarta la suya al terminar cada job, así que el caso
    normal ya está cubierto; esto limpia lo que queda cuando el proceso muere en
    medio de uno. Va contra el coordinador y no contra el storage: el storage es
    dependencia suya, no de este servicio.
    """
    return await ocr_gateway.request("post", "/ocr/maintenance/purge-temp")


async def _readyz() -> tuple[dict, bool]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{settings.OCR_SERVICE_URL}/ocr/readyz")
        payload = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"error": str(e)}, False

    # Un 503 del coordinador trae su diagnóstico dentro de `detail`: es
    # información para el panel, no un error que haya que tapar.
    if r.status_code == 503 and isinstance(payload, dict):
        payload = payload.get("detail", payload)

    return payload if isinstance(payload, dict) else {}, r.status_code == 200


async def _engines() -> List[EngineStatus]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{settings.OCR_SERVICE_URL}/ocr/engines")
        return [EngineStatus(**engine) for engine in r.json()] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, TypeError):
        return []
