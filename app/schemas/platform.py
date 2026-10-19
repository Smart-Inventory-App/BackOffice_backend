from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class CategoryCount(BaseModel):
    label: str
    count: int


class DashboardStats(BaseModel):
    total_users: int
    active_users: int
    inactive_users: int
    new_users_last_7d: int
    total_products: int
    products_with_image: int
    low_stock_products: int          # stock <= 1
    expiring_soon_products: int      # vencimiento within next 7 days
    expired_products: int
    products_by_category: List[CategoryCount]
    top_users_by_products: List["UserProductCount"]
    products_per_day_last_14d: List["DayCount"]


class DayCount(BaseModel):
    day: str
    count: int


class UserProductCount(BaseModel):
    user_id: str
    email: Optional[str] = None
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
    created_at: Optional[datetime] = None
    owner: ProductOwner


class AppUserDetail(AppUserResponse):
    products: List[AdminProductResponse]


class PaginatedUsers(BaseModel):
    items: List[AppUserResponse]
    total: int


class PaginatedProducts(BaseModel):
    items: List[AdminProductResponse]
    total: int


class MyStats(BaseModel):
    total_products: int
    products_with_image: int
    low_stock_products: int
    expiring_soon_products: int
    expired_products: int
    products_by_category: List[CategoryCount]


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


# Los mismos máximos que el alta de `app_backend`: un producto cargado desde el
# backoffice tiene que poder editarse y escanearse desde la app.
MAX_NOMBRE = 20
MAX_CATEGORIA = 20
MAX_DESCRIPCION = 400


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
    stock_minimo: Optional[int] = Field(None, gt=0)
    dias_alerta_vencimiento: Optional[int] = Field(None, gt=0)


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
    stock_minimo: Optional[int] = Field(None, gt=0)
    dias_alerta_vencimiento: Optional[int] = Field(None, gt=0)


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


class MyDefaults(BaseModel):
    last_categoria: Optional[str] = None
    last_ubicacion: Optional[str] = None


class MyProfileResponse(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    stock_minimo: int
    dias_alerta_vencimiento: int


class MyProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    stock_minimo: Optional[int] = Field(None, gt=0)
    dias_alerta_vencimiento: Optional[int] = Field(None, gt=0)


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


class MyMetrics(BaseModel):
    period_days: int
    total_consumido: int
    total_entradas: int
    consumo_por_dia: List[ConsumoPorDia]
    top_productos: List[ProductoConsumo]
    productos_menos_usados: List[ProductoConsumo]


class ActivityEntry(BaseModel):
    id: str
    kind: str  # "status" | "role" | "login" | "product_deleted"
    actor_email: Optional[str] = None
    target_user_id: Optional[str] = None
    target_email: Optional[str] = None
    description: str
    created_at: Optional[datetime] = None


class PaginatedActivity(BaseModel):
    items: List[ActivityEntry]
    total: int


DashboardStats.model_rebuild()
