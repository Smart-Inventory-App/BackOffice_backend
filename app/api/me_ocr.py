"""
Carga de productos por foto, del lado del usuario.

Es el mismo módulo de OCR que usa el panel de administración, pero con otra
finalidad: acá no se diagnostica el módulo, se arma un borrador que el usuario va
a corregir. De ahí las dos diferencias con `/admin/ocr`:

* **el token que viaja es el del usuario**, para que la cuota por minuto y la
  propiedad del job queden a su nombre;
* **la lectura vuelve emparejada** con el inventario de quien escanea, porque un
  nombre suelto no sirve para decidir a qué producto sumarle o restarle stock.

Nada se guarda desde acá. La foto la sube el coordinador a la carpeta temporal y
la descarta al terminar el job; lo único que persiste es lo que el usuario
confirma después, contra los endpoints de alta y de salida de siempre.
"""
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.me import _log_activity, _log_consumption
from app.core import matching, ocr_audit, ocr_gateway
from app.core.database import get_db
from app.core.security import get_bearer_token, get_current_user
from app.models.app_models import AuthUser, Product
from app.schemas.ocr import (
    ProductDraft,
    ProductMatch,
    ReceiptInfo,
    SalidaOCRRequest,
    SalidaOCRResult,
    ScanDocType,
    ScanResult,
    ScanState,
)

router = APIRouter(prefix="/me/products/ocr", tags=["me-ocr"])

# Máximos de `MyProductCreate`: el borrador tiene que entrar en el formulario que
# lo va a recibir, o el usuario confirma algo que después el alta rechaza.
MAX_NOMBRE = 20
MAX_DESCRIPCION = 400

MOTIVO_COMPROBANTE = "Documento Comercial"


@router.post("/scan", response_model=ScanState)
async def scan(
    response: Response,
    file: UploadFile = File(...),
    doc_type: ScanDocType = Form(default=ScanDocType.PRODUCT_LABEL),
    token: str = Depends(get_bearer_token),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """
    Manda una foto al módulo de OCR.

    Un código de barras vuelve resuelto en esta misma llamada (`200`, con
    `status: "done"`) y ya emparejado con el producto del usuario que lo tenga:
    no pasa por ningún motor —lo decodifica el coordinador— y encolarlo sería
    pagar la subida, la cola y el polling por un trabajo de dos décimas.

    Los demás tipos vuelven encolados (`202`) y hay que consultar el estado.
    """
    content = await ocr_gateway.read_image(file)
    envio = {
        "files": {"file": (file.filename or "scan.jpg", content, file.content_type)},
        "data": {"doc_type": doc_type.value},
    }

    if doc_type is ScanDocType.BARCODE:
        try:
            lectura = await ocr_gateway.request("post", "/ocr/sync/upload", token=token, **envio)
        except HTTPException as e:
            await ocr_audit.registrar_falla(
                db, e, user_email=current.email, user_id=str(current.id),
                origen=ocr_audit.ORIGEN_APP, doc_type=doc_type.value,
            )
            raise
        await run_in_threadpool(
            ocr_audit.registrar,
            db,
            user_email=current.email,
            user_id=str(current.id),
            origen=ocr_audit.ORIGEN_APP,
            doc_type=doc_type.value,
            result=lectura["result"],
        )
        return ScanState(
            status="done",
            doc_type=doc_type,
            result=await _resolver(doc_type, lectura["result"], db, current),
        )

    creado = await ocr_gateway.request("post", "/ocr/scan", token=token, **envio)
    response.status_code = status.HTTP_202_ACCEPTED
    return ScanState(
        job_id=creado["job_id"],
        status=creado.get("status", "queued"),
        doc_type=doc_type,
    )


@router.get("/scan/{job_id}", response_model=ScanState)
async def get_scan(
    job_id: str,
    token: str = Depends(get_bearer_token),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """
    Estado del escaneo y, cuando termina, el borrador emparejado.

    Mientras está en la fila trae el lugar y la espera estimada, para que la
    pantalla pueda decir «3º de 7, ~2 min» en lugar de girar sin decir nada. El
    coordinador sólo devuelve el job a quien lo creó: un id ajeno da 404.
    """
    job = await ocr_gateway.request("get", f"/ocr/jobs/{job_id}", token=token)

    doc_type = ScanDocType(job["doc_type"])
    resultado = job.get("result")

    if job["status"] in ("done", "failed"):
        await run_in_threadpool(
            ocr_audit.registrar,
            db,
            user_email=current.email,
            user_id=str(current.id),
            origen=ocr_audit.ORIGEN_APP,
            doc_type=doc_type.value,
            result=resultado,
            job_id=job["job_id"],
            status=job["status"],
            error=job.get("error"),
        )

    return ScanState(
        job_id=job["job_id"],
        status=job["status"],
        doc_type=doc_type,
        result=await _resolver(doc_type, resultado, db, current) if resultado else None,
        error=job.get("error"),
        queue_position=job.get("queue_position"),
        estimated_wait_seconds=job.get("estimated_wait_seconds"),
    )


@router.post("/salida", response_model=SalidaOCRResult)
def salida_por_comprobante(
    data: SalidaOCRRequest,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """
    Descarga varios productos de un mismo comprobante.

    Llega con los `product_id` que el usuario ya confirmó contra las
    coincidencias, no con nombres: volver a emparejar acá sería decidir de nuevo
    algo que la persona ya decidió mirando la foto.

    El movimiento queda como consumo —que es lo que es, para las métricas y para
    el ledger— con el motivo `Documento Comercial`. Cada renglón se procesa por su
    cuenta y los que no se pueden aplicar vuelven explicados: que a uno le falte
    stock no puede dejar el comprobante a medio aplicar sin que se sepa cuál falló.
    """
    uid = str(current.id)
    motivo = f"{MOTIVO_COMPROBANTE} - {data.comercio}" if data.comercio else MOTIVO_COMPROBANTE

    procesados = 0
    errores: list[str] = []

    for item in data.items:
        product = (
            db.query(Product)
            .filter(Product.id == item.product_id, Product.user_id == uid)
            .first()
        )
        if not product:
            errores.append(f"Producto #{item.product_id}: no encontrado")
            continue
        if item.quantity > product.stock:
            errores.append(
                f'Producto #{item.product_id}: "{product.nombre}" tiene {product.stock} '
                f"unidades y pediste sacar {item.quantity}"
            )
            continue

        product.stock -= item.quantity
        try:
            db.commit()
        except SQLAlchemyError as e:
            db.rollback()
            errores.append(f"Producto #{item.product_id}: no se pudo registrar la salida ({e})")
            continue
        db.refresh(product)

        _log_consumption(
            db, product.id, uid, item.quantity, "consumed",
            actor_email=current.email, note=motivo,
        )
        _log_activity(
            db, "product_updated", current.email, uid, current.email,
            f'{current.email} sacó {item.quantity} unidades de "{product.nombre}" '
            f"({MOTIVO_COMPROBANTE}, stock: {product.stock}) desde el dashboard",
        )
        procesados += 1

    return SalidaOCRResult(procesados=procesados, fallidos=len(errores), errores=errores)


# ── de la lectura del coordinador al borrador del formulario ──────────────────

async def _resolver(
    doc_type: ScanDocType, result: dict[str, Any], db: Session, current: AuthUser
) -> ScanResult:
    """Traduce y empareja fuera del event loop.

    Emparejar es leer el inventario del usuario y compararlo texto contra texto:
    consultas bloqueantes y CPU. Hacerlo dentro del loop de un handler `async`
    frenaría a todos los demás escaneos que están esperando al coordinador, que
    es justamente lo que este servicio hace la mayor parte del tiempo.
    """
    return await run_in_threadpool(_traducir, doc_type, result, db, str(current.id))



def _traducir(doc_type: ScanDocType, result: dict[str, Any], db: Session, uid: str) -> ScanResult:
    """
    Arma el borrador y lo empareja con el inventario del usuario.

    El criterio es que el borrador no inventa: lo que el OCR no leyó queda vacío.
    Rellenar un hueco con un valor plausible es peor que dejarlo vacío, porque se
    confirma el formulario sin releerlo.
    """
    fields = result.get("fields") or {}
    advertencias = list(result.get("warnings") or [])
    inventario = _inventario(db, uid)

    items: list[ProductDraft] = []
    comprobante: Optional[ReceiptInfo] = None
    draft: Optional[ProductDraft] = None

    if doc_type is ScanDocType.RECEIPT:
        items, comprobante = _de_comprobante(
            fields, result.get("items") or [], advertencias, inventario
        )
    else:
        draft = _de_una_foto(doc_type, fields, advertencias, inventario)

    return ScanResult(
        doc_type=doc_type,
        draft=draft,
        items=items,
        comprobante=comprobante,
        needs_review=bool(result.get("needs_review", True)),
        confianza=float(result.get("document_confidence") or 0.0),
        texto_legible=result.get("texto_legible") or "",
        advertencias=advertencias,
        sin_coincidencia=sum(1 for item in items if not item.coincidencias),
    )


def _inventario(db: Session, uid: str) -> list[Product]:
    """Los productos del usuario, que son el universo contra el que se empareja."""
    return db.query(Product).filter(Product.user_id == uid).all()


def _de_una_foto(
    doc_type: ScanDocType,
    fields: dict[str, Any],
    advertencias: list[str],
    inventario: list[Product],
) -> ProductDraft:
    """Un producto por foto: etiqueta, sello de vencimiento o código de barras."""
    draft = ProductDraft()
    confianzas: dict[str, float] = {}

    def tomar(campo_ocr: str, campo_draft: str, valor) -> None:
        if valor is None:
            return
        setattr(draft, campo_draft, valor)
        confianzas[campo_draft] = _confianza(fields, campo_ocr)

    if doc_type is ScanDocType.PRODUCT_LABEL:
        leido = _texto(fields, "nombre_producto")
        draft.texto_leido = leido
        tomar("nombre_producto", "nombre", _recortar(leido, MAX_NOMBRE, "nombre", advertencias))
        tomar("marca", "marca", _texto(fields, "marca"))
        tomar(
            "detalles",
            "descripcion",
            _recortar(_texto(fields, "detalles"), MAX_DESCRIPCION, "descripcion", advertencias),
        )
        _emparejar(draft, leido, inventario)

    elif doc_type is ScanDocType.EXPIRY_DATE:
        tomar("fecha_vencimiento", "vencimiento", _fecha(fields, "fecha_vencimiento"))

    elif doc_type is ScanDocType.BARCODE:
        codigo = _texto(fields, "codigo")
        tomar("codigo", "codigo_barras", codigo)
        tomar("pais_registro", "pais_registro", _texto(fields, "pais_registro"))
        draft.texto_leido = codigo
        _emparejar_por_codigo(draft, codigo, inventario)

    draft.confianzas = confianzas
    return draft


def _de_comprobante(
    fields: dict[str, Any],
    items: list[dict[str, Any]],
    advertencias: list[str],
    inventario: list[Product],
) -> tuple[list[ProductDraft], ReceiptInfo]:
    """
    Varios productos por foto: un borrador por renglón, más la cabecera.

    La cantidad del renglón es el stock del movimiento: es lo que el usuario acaba
    de comprar, o lo que el remito descarga. La categoría y el vencimiento no
    salen de un comprobante, así que quedan vacíos.
    """
    borradores: list[ProductDraft] = []

    for item in items:
        leido = _valor(item.get("nombre_producto"))
        if not leido:
            continue

        cantidad = _valor(item.get("cantidad"))
        legible = isinstance(cantidad, (int, float)) and cantidad >= 1

        draft = ProductDraft(
            nombre=_recortar(str(leido), MAX_NOMBRE, "nombre", advertencias),
            stock=int(cantidad) if legible else None,
            texto_leido=str(leido),
            confianzas={
                "nombre": _confianza(item, "nombre_producto"),
                "stock": _confianza(item, "cantidad"),
            },
        )
        _emparejar(draft, str(leido), inventario)
        borradores.append(draft)

    comprobante = ReceiptInfo(
        comercio=_texto(fields, "comercio"),
        fecha_compra=_fecha(fields, "fecha_compra"),
        total=_numero(fields, "total"),
    )

    return borradores, comprobante


def _emparejar(draft: ProductDraft, leido: Optional[str], inventario: list[Product]) -> None:
    """Cuelga del borrador los productos del usuario que podrían ser éste."""
    encontrados = matching.buscar(leido, inventario)
    draft.coincidencias = [_a_match(p, puntaje) for p, puntaje in encontrados]

    if encontrados and encontrados[0][1] >= matching.UMBRAL_CONFIABLE:
        draft.coincidencia_id = encontrados[0][0].id


def _emparejar_por_codigo(
    draft: ProductDraft, codigo: Optional[str], inventario: list[Product]
) -> None:
    """
    Un código de barras no se parece a un producto: es ese producto.

    Por eso no pasa por el emparejamiento difuso ni por el umbral. Si no hay
    ninguno con ese código, el borrador vuelve sin coincidencias y el formulario
    abre un alta nueva con el código ya cargado.
    """
    if not codigo:
        return
    # Dos lotes del mismo producto comparten código y difieren en el vencimiento.
    # Van primero los que vencen antes —y los sin fecha al final—, que es el que
    # conviene sacar primero y el mismo orden que usa la búsqueda por código. Sin
    # ordenar, la salida por código descontaba de un lote cualquiera.
    iguales = sorted(
        (p for p in inventario if p.codigo_barras == codigo),
        key=lambda p: (p.vencimiento is None, p.vencimiento or date.max, p.id),
    )
    draft.coincidencias = [_a_match(p, 1.0, por_codigo=True) for p in iguales]
    if iguales:
        draft.coincidencia_id = iguales[0].id


def _a_match(product: Product, score: float, por_codigo: bool = False) -> ProductMatch:
    return ProductMatch(
        id=product.id,
        nombre=product.nombre,
        marca=product.marca,
        categoria=product.categoria,
        ubicacion=product.ubicacion,
        vencimiento=product.vencimiento,
        stock=product.stock or 0,
        codigo_barras=product.codigo_barras,
        score=round(score, 3),
        por_codigo=por_codigo,
    )


# ── lectura de un ExtractedField ──────────────────────────────────────────────

def _valor(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else None


def _confianza(contenedor: dict[str, Any], nombre: str) -> float:
    field = contenedor.get(nombre)
    return float(field.get("confidence") or 0.0) if isinstance(field, dict) else 0.0


def _texto(fields: dict[str, Any], nombre: str) -> Optional[str]:
    valor = _valor(fields.get(nombre))
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _numero(fields: dict[str, Any], nombre: str) -> Optional[float]:
    valor = _valor(fields.get(nombre))
    return float(valor) if isinstance(valor, (int, float)) else None


def _fecha(fields: dict[str, Any], nombre: str) -> Optional[date]:
    """El coordinador normaliza toda fecha a ISO YYYY-MM-DD antes de devolverla."""
    texto = _texto(fields, nombre)
    if texto is None:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _recortar(
    texto: Optional[str], maximo: int, campo: str, advertencias: list[str]
) -> Optional[str]:
    """
    Ajusta el texto al máximo del formulario, cortando por palabra si se puede.

    Cortar a la mitad de una palabra da un nombre que el usuario no reconoce, y que
    el campo llegue recortado se avisa: quien revisa el borrador tiene que saber
    que ahí falta texto. El texto completo sigue en `texto_leido`.
    """
    if texto is None or len(texto) <= maximo:
        return texto

    corte = texto.rfind(" ", 0, maximo + 1)
    advertencias.append(f"{campo}_recortado")

    return texto[: corte if corte > 0 else maximo].rstrip()
