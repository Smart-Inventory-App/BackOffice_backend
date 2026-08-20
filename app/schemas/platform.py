from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


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
    is_active: bool
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
    created_at: Optional[datetime] = None
    merged: bool = False

    class Config:
        from_attributes = True


class MyProductCreate(BaseModel):
    nombre: str
    marca: Optional[str] = None
    categoria: str
    stock: int
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = None
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None


class PaginatedMyProducts(BaseModel):
    items: List[MyProductResponse]
    total: int


class ProductSuggestion(BaseModel):
    nombre: str
    marca: Optional[str] = None
    categoria: str
    ubicacion: Optional[str] = None


class MyDefaults(BaseModel):
    last_categoria: Optional[str] = None
    last_ubicacion: Optional[str] = None


class BulkProductRow(BaseModel):
    row_number: int
    nombre: str
    marca: Optional[str] = None
    categoria: str
    stock: int
    vencimiento: Optional[date] = None
    descripcion: Optional[str] = None
    imagen_url: Optional[str] = None
    ubicacion: Optional[str] = None


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


class BulkCommitResult(BaseModel):
    created: int
    merged: int
    failed: int
    errors: List[str]


DashboardStats.model_rebuild()
