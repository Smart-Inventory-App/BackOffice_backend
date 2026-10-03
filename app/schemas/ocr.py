"""
Contratos del flujo de carga por foto.

Lo que distingue a estos borradores de los del alta normal es que **no se
guardan**: son lo que el usuario ve, corrige y recién entonces confirma contra
los endpoints de siempre. Por eso cada campo viaja con su confianza y con el
texto crudo del que salió: quien revisa tiene que poder ver de dónde vino cada
cosa antes de darla por buena.
"""
from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ScanDocType(str, Enum):
    """Los cuatro tipos de foto que acepta el módulo, igual que el coordinador."""

    PRODUCT_LABEL = "product_label"
    EXPIRY_DATE = "expiry_date"
    RECEIPT = "receipt"
    BARCODE = "barcode"


class ProductMatch(BaseModel):
    """Un producto del inventario del usuario que podría ser el escaneado."""

    id: int
    nombre: str
    marca: Optional[str] = None
    categoria: Optional[str] = None
    ubicacion: Optional[str] = None
    vencimiento: Optional[date] = None
    stock: int = 0
    codigo_barras: Optional[str] = None

    score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Cuánto se parece al texto leído. 1.0 es coincidencia exacta y "
            "también es lo que devuelve un código de barras, que no se parece: "
            "es el mismo producto."
        ),
    )
    por_codigo: bool = Field(
        default=False,
        description="La coincidencia salió del código de barras, no del nombre.",
    )


class ProductDraft(BaseModel):
    """Borrador de un producto: lo leído, lo que se le parece y qué tan seguro es."""

    nombre: Optional[str] = None
    marca: Optional[str] = None
    descripcion: Optional[str] = None
    vencimiento: Optional[date] = None
    codigo_barras: Optional[str] = None
    pais_registro: Optional[str] = None
    stock: Optional[int] = None

    confianzas: dict[str, float] = Field(default_factory=dict)

    texto_leido: Optional[str] = Field(
        default=None,
        description=(
            "El nombre tal como lo leyó el OCR, sin recortar ni corregir. Es lo "
            "que se muestra para editar cuando no hay ninguna coincidencia."
        ),
    )
    coincidencias: List[ProductMatch] = Field(
        default_factory=list,
        description="Productos del usuario que podrían ser éste, de mejor a peor.",
    )
    coincidencia_id: Optional[int] = Field(
        default=None,
        description=(
            "La coincidencia lo bastante buena como para preseleccionarla. Sigue "
            "siendo una sugerencia: el formulario la deja cambiar."
        ),
    )


class ReceiptInfo(BaseModel):
    comercio: Optional[str] = None
    fecha_compra: Optional[date] = None
    total: Optional[float] = None


class ScanResult(BaseModel):
    doc_type: ScanDocType
    draft: Optional[ProductDraft] = None
    items: List[ProductDraft] = Field(default_factory=list)
    comprobante: Optional[ReceiptInfo] = None

    needs_review: bool = True
    confianza: float = 0.0
    texto_legible: str = ""
    advertencias: List[str] = Field(default_factory=list)

    sin_coincidencia: int = Field(
        default=0,
        description=(
            "Renglones del comprobante que no se pudieron atar a ningún producto "
            "del usuario. La salida los necesita para avisar cuántos quedaron "
            "afuera en vez de descargar de menos en silencio."
        ),
    )


class ScanState(BaseModel):
    """Estado de un escaneo. Un código de barras nace `done`; el resto, `queued`."""

    job_id: Optional[str] = None
    status: str = "queued"
    doc_type: ScanDocType
    result: Optional[ScanResult] = None
    error: Optional[str] = None

    queue_position: Optional[int] = None
    estimated_wait_seconds: Optional[int] = None


class SalidaOCRItem(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)


class SalidaOCRRequest(BaseModel):
    """Descarga de varios productos leídos de un mismo comprobante."""

    items: List[SalidaOCRItem] = Field(..., min_length=1)
    comercio: Optional[str] = Field(
        default=None,
        description="Si el OCR lo leyó, queda en el motivo del movimiento.",
    )


class SalidaOCRResult(BaseModel):
    procesados: int
    fallidos: int
    errores: List[str] = Field(default_factory=list)
