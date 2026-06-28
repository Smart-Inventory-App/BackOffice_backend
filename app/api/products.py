from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, Product
from app.schemas.platform import AdminProductResponse, PaginatedProducts, ProductOwner

router = APIRouter(prefix="/admin/products", tags=["admin-products"])


def _email_map(db: Session) -> dict[str, str]:
    return {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}


def _to_response(p: Product, emails: dict[str, str]) -> AdminProductResponse:
    return AdminProductResponse(
        id=p.id,
        nombre=p.nombre,
        marca=p.marca,
        categoria=p.categoria,
        stock=p.stock,
        vencimiento=p.vencimiento,
        descripcion=p.descripcion,
        imagen_url=p.imagen_url,
        ubicacion=p.ubicacion,
        created_at=p.created_at,
        owner=ProductOwner(user_id=p.user_id, email=emails.get(p.user_id)),
    )


@router.get("/", response_model=PaginatedProducts)
def list_products(
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    only_with_image: bool = Query(False),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = db.query(Product)
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))
    if only_with_image:
        query = query.filter(Product.imagen_url.isnot(None), Product.imagen_url != "")

    total = query.count()
    products = query.order_by(Product.created_at.desc()).offset(offset).limit(limit).all()
    emails = _email_map(db)
    return PaginatedProducts(items=[_to_response(p, emails) for p in products], total=total)


@router.get("/{product_id}", response_model=AdminProductResponse)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    return _to_response(p, _email_map(db))


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    db.delete(p)
    db.commit()
