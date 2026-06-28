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
    created_at: Optional[datetime] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    product_count: int


class UserStatusUpdate(BaseModel):
    is_active: bool


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


DashboardStats.model_rebuild()
