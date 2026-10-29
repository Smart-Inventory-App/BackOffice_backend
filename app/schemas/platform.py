from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.alerts import AlertOut


class CategoryCount(BaseModel):
    label: str
    count: int


class DayCount(BaseModel):
    day: str
    count: int


class AppUserResponse(BaseModel):
    id: str
    email: Optional[str] = None
    is_active: bool
    is_admin: bool = False
    created_at: Optional[datetime] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    product_count: int


class UserStatusUpdate(BaseModel):
    is_active: bool
    reason: Optional[str] = None


class AuditEntry(BaseModel):
    id: int
    user_id: str
    changed_by: str
    kind: str  # "status" | "role"
    is_active: Optional[bool] = None  # solo para kind == "status"
    action: Optional[str] = None      # "granted" | "revoked", solo para kind == "role"
    reason: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductOwner(BaseModel):
    user_id: str
    email: Optional[str] = None
    # false si la cuenta del usuario está deshabilitada
    is_active: bool = True


class AdminProductResponse(BaseModel):
    id: int
    nombre: str
    marca: Optional[str] = None
    categoria: str
    stock: int
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = None
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None
    codigo_barras: Optional[str] = None
    stock_minimo: Optional[int] = None
    dias_alerta_vencimiento: Optional[int] = None
    created_at: Optional[datetime] = None
    # La alerta más grave que tiene hoy el producto (critical | warning) y de qué tipos son; vacío si no tiene
    alert_level: Optional[str] = None
    alert_types: List[str] = []
    # Las alertas del producto con todo lo necesario para mostrarlas (color, título y descripción)
    alerts: List[AlertOut] = []
    owner: ProductOwner


class AdminProductGroupSummary(BaseModel):
    """Todos los productos con el mismo nombre y categoría (la marca puede variar), con su stock total."""

    nombre: str
    categoria: str
    stock_total: int
    lotes: int
    usuarios: int
    proximo_vencimiento: Optional[date] = None


class AdminProductGroupDetail(AdminProductGroupSummary):
    productos: List[AdminProductResponse]


class AppUserDetail(AppUserResponse):
    products: List[AdminProductResponse]
    total_units: int = 0
    critical_alerts: int = 0
    warning_alerts: int = 0
    consumed_last_30d: int = 0
    restocked_last_30d: int = 0
    alerts: List[AlertOut] = []


class PaginatedUsers(BaseModel):
    items: List[AppUserResponse]
    total: int


class PaginatedProducts(BaseModel):
    items: List[AdminProductResponse]
    total: int


class MyStats(BaseModel):
    total_products: int
    # Suma del stock de todos los productos
    total_units: int = 0
    products_with_image: int
    # Productos con una alerta crítica de stock mínimo y de vencimiento: los mismos números que la app
    low_stock_products: int
    expiring_soon_products: int
    expired_products: int
    critical_alerts: int = 0
    warning_alerts: int = 0
    products_by_category: List[CategoryCount]
    # Unidades en stock por categoría, productos por ubicación y cuándo vencen: los gráficos del dashboard
    stock_by_category: List[CategoryCount] = []
    products_by_location: List[CategoryCount] = []
    # Unidades en stock por marca (solo los productos que tienen marca)
    stock_by_brand: List[CategoryCount] = []
    expiry_buckets: List[CategoryCount] = []


class MyProductResponse(BaseModel):
    id: int
    nombre: str
    marca: Optional[str] = None
    categoria: str
    stock: int
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = None
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None
    codigo_barras: Optional[str] = None
    stock_minimo: Optional[int] = None
    dias_alerta_vencimiento: Optional[int] = None
    created_at: Optional[datetime] = None
    merged: bool = False

    class Config:
        from_attributes = True


class StockChangeOut(BaseModel):
    """Un cambio del stock de un producto: una entrada, una salida o una modificación, con el stock antes y después."""

    id: str
    kind: Literal["entrada", "salida", "modificacion"]
    stock_before: int
    stock_after: int
    quantity: int
    occurred_at: datetime
    producto: MyProductResponse


# Los mismos máximos que el alta de `app_backend`: un producto cargado desde el
# backoffice tiene que poder editarse y escanearse desde la app.
MAX_NOMBRE = 20
MAX_CATEGORIA = 20
MAX_DESCRIPCION = 400
# Las reglas propias de un producto, con los mismos topes que la app
MAX_STOCK_MINIMO = 9_999_999
MAX_DIAS_ALERTA_VENCIMIENTO = 999


class MyProductCreate(BaseModel):
    nombre: str = Field(..., max_length=MAX_NOMBRE)
    marca: Optional[str] = None
    categoria: str = Field(..., max_length=MAX_CATEGORIA)
    stock: int = Field(..., gt=0)
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = Field(None, max_length=MAX_DESCRIPCION)
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None
    codigo_barras: Optional[str] = Field(
        default=None,
        description="GTIN de 8, 12, 13 o 14 dígitos. Se valida el verificador y se normaliza a 13.",
    )
    stock_minimo: Optional[int] = Field(None, ge=1, le=MAX_STOCK_MINIMO)
    dias_alerta_vencimiento: Optional[int] = Field(None, ge=1, le=MAX_DIAS_ALERTA_VENCIMIENTO)


class MyProductUpdate(BaseModel):
    nombre: Optional[str] = Field(None, max_length=MAX_NOMBRE)
    marca: Optional[str] = None
    categoria: Optional[str] = Field(None, max_length=MAX_CATEGORIA)
    stock: Optional[int] = Field(None, ge=0)
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = Field(None, max_length=MAX_DESCRIPCION)
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None
    codigo_barras: Optional[str] = None
    stock_minimo: Optional[int] = Field(None, ge=1, le=MAX_STOCK_MINIMO)
    dias_alerta_vencimiento: Optional[int] = Field(None, ge=1, le=MAX_DIAS_ALERTA_VENCIMIENTO)


class PaginatedMyProducts(BaseModel):
    items: List[MyProductResponse]
    total: int


class MyBulkEditRequest(BaseModel):
    product_ids: List[int]
    categoria: Optional[str] = None
    ubicacion: Optional[str] = None
    marca: Optional[str] = None


class MyBulkEditResult(BaseModel):
    updated: int


class MyBulkDeleteRequest(BaseModel):
    product_ids: List[int]


class MyBulkDeleteResult(BaseModel):
    deleted: int


class ProductSuggestion(BaseModel):
    id: int
    nombre: str
    marca: Optional[str] = None
    categoria: str
    ubicacion: Optional[str] = None
    vencimiento: Optional[date] = None
    stock: int = 0
    stock_minimo: Optional[int] = None
    dias_alerta_vencimiento: Optional[int] = None
    codigo_barras: Optional[str] = None
    imagen_url: Optional[str] = None


class MyDefaults(BaseModel):
    last_categoria: Optional[str] = None
    last_ubicacion: Optional[str] = None


# Límites del perfil, los mismos que valida la app: nombre, apellido y usuario hasta 20 caracteres; los días de las
# alertas de 2 a 90; la hora del chequeo diario de 0 a 23 (hora argentina).
PROFILE_TEXT_MAX = 20
ALERT_DAYS_MIN = 2
ALERT_DAYS_MAX = 90

UserType = Literal["individuo", "comercio"]


class MyProfileResponse(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    # Días de cobertura mínimos (stock mínimo) y días de anticipación del vencimiento, para todos los productos
    stock_minimo: int
    dias_alerta_vencimiento: int
    # Hora argentina (0 a 23) del chequeo diario de alertas
    alert_check_hour: int = 5
    user_type: UserType = "individuo"


class MyProfileUpdate(BaseModel):
    first_name: Optional[str] = Field(None, max_length=PROFILE_TEXT_MAX)
    last_name: Optional[str] = Field(None, max_length=PROFILE_TEXT_MAX)
    username: Optional[str] = Field(None, max_length=PROFILE_TEXT_MAX)
    stock_minimo: Optional[int] = Field(None, ge=ALERT_DAYS_MIN, le=ALERT_DAYS_MAX)
    dias_alerta_vencimiento: Optional[int] = Field(None, ge=ALERT_DAYS_MIN, le=ALERT_DAYS_MAX)
    alert_check_hour: Optional[int] = Field(None, ge=0, le=23)
    user_type: Optional[UserType] = None


class BulkProductRow(BaseModel):
    row_number: int
    nombre: str
    marca: Optional[str] = None
    categoria: str
    stock: int
    codigo_barras: Optional[str] = None
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = None
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None
    stock_minimo: Optional[int] = None
    dias_alerta_vencimiento: Optional[int] = None


class BulkParsedRow(BaseModel):
    row_number: int
    data: Optional[BulkProductRow] = None
    valid: bool
    errors: List[str] = []
    action: Optional[str] = None  # "create" | "merge"
    existing_stock: Optional[int] = None


class BulkParseResponse(BaseModel):
    rows: List[BulkParsedRow]


class BulkCommitRequest(BaseModel):
    rows: List[BulkProductRow]
    mode: str = "entrada"  # "entrada" | "salida"
    origen: str = Field(
        "Carga masiva",
        max_length=60,
        description=(
            "De dónde salieron estas filas. Queda como motivo del movimiento y en "
            "el historial: la carga por archivo y la lectura de un comprobante "
            "usan el mismo commit y hay que poder distinguirlas después."
        ),
    )


class BulkCommitResult(BaseModel):
    created: int
    merged: int
    failed: int
    errors: List[str]


class RestockRequest(BaseModel):
    """Stock que entra a un producto que el usuario ya tiene cargado."""
    quantity: int = Field(..., gt=0, le=999_999)


class SalidaRequest(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)
    event_type: str = Field("consumed", pattern="^(consumed|expired|sold|rented)$")
    note: Optional[str] = None


class ConsumptionEventOut(BaseModel):
    id: int
    quantity: int
    event_type: str
    consumed_at: datetime
    note: Optional[str] = None
    actor_email: Optional[str] = None
    reverses_event_id: Optional[int] = None
    reversed: bool = False


class EventCorrection(BaseModel):
    quantity: int = Field(..., gt=0)
    event_type: str = Field(..., pattern="^(consumed|expired|sold|rented|restocked)$")
    note: Optional[str] = None


class ConsumoPorDia(BaseModel):
    day: str
    quantity: int


class ProductoConsumo(BaseModel):
    product_id: int
    nombre: str
    cantidad: int
    # Solo en el ranking de toda la plataforma: a qué usuario pertenece el producto
    owner_email: Optional[str] = None


class MyMetrics(BaseModel):
    period_days: int
    total_consumido: int
    total_entradas: int
    consumo_por_dia: List[ConsumoPorDia]
    top_productos: List[ProductoConsumo]
    # Todo lo que ingresó al stock en el período: las reposiciones de productos que ya existían y el stock con el que se
    # cargaron los productos nuevos. Por día (días argentinos) y en total.
    total_ingresos: int = 0
    ingresos_por_dia: List[ConsumoPorDia] = []


class ActivityEntry(BaseModel):
    id: str
    kind: str  # "status" | "role" | "login" | "registered" | "product_created" | "product_exit" | "product_updated" | "product_deleted"
    actor_email: Optional[str] = None
    target_user_id: Optional[str] = None
    target_email: Optional[str] = None
    description: str
    created_at: Optional[datetime] = None


class PaginatedActivity(BaseModel):
    items: List[ActivityEntry]
    total: int


# ── Resumen de la plataforma (dashboard del administrador) ───────────────────

class UsersOverview(BaseModel):
    total: int
    # Con la cuenta habilitada
    active: int
    new_in_period: int
    # Usuarios que hicieron algo en el período (iniciaron sesión o movieron stock)
    active_in_period: int
    # Los habilitados que ya cargaron al menos un producto
    with_products: int
    registrations_per_day: List["DayCount"]


class ActivityDay(BaseModel):
    day: str
    logins: int = 0
    entradas: int = 0
    salidas: int = 0


class InventoryOverview(BaseModel):
    products: int
    units: int
    with_image: int
    without_expiry: int


class ActiveUserRow(BaseModel):
    user_id: str
    email: Optional[str] = None
    # El nombre y el usuario que la persona tiene en su perfil
    name: Optional[str] = None
    username: Optional[str] = None
    # Movimientos de stock del período: el total y cuántos fueron entradas y cuántos salidas
    movements: int
    entries: int
    exits: int
    products: int
    units: int
    is_active: bool = True


class OcrOverview(BaseModel):
    runs: int
    succeeded: int
    avg_ms: Optional[int] = None
    avg_confidence: Optional[float] = None
    by_type: List[dict]


class PlatformOverview(BaseModel):
    period_days: int
    users: UsersOverview
    activity_per_day: List[ActivityDay]
    inventory: InventoryOverview
    most_active_users: List[ActiveUserRow]
    ocr: OcrOverview


PlatformOverview.model_rebuild()
