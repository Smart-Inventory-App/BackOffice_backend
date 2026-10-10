from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session, Query as SAQuery

from app.core.database import get_db
from app.core.export import products_to_csv, products_to_xlsx
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import Alert, AuthUser, Product
from app.models.audit import AdminActivityLog
from app.schemas.platform import AdminProductResponse, PaginatedProducts, ProductOwner

router = APIRouter(prefix="/admin/products", tags=["admin-products"])

SORTABLE_FIELDS = {
    "nombre": Product.nombre,
    "stock": Product.stock,
    "vencimiento": Product.vencimiento,
    "categoria": Product.categoria,
}


def _email_map(db: Session) -> dict[str, str]:
    return {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}


def _apply_filters(
    query: SAQuery,
    search: Optional[str],
    categoria: Optional[str],
    user_id: Optional[str],
    only_with_image: bool,
    vencido: Optional[bool],
    bajo_stock: bool,
) -> SAQuery:
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))
    if user_id:
        query = query.filter(Product.user_id == user_id)
    if only_with_image:
        query = query.filter(Product.imagen_url.isnot(None), Product.imagen_url != "")
    if vencido is True:
        query = query.filter(Product.vencimiento.isnot(None), Product.vencimiento < date.today())
    elif vencido is False:
        query = query.filter(
            (Product.vencimiento.is_(None)) | (Product.vencimiento >= date.today())
        )
    if bajo_stock:
        query = query.filter(Product.stock <= 1)
    return query


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
        codigo_barras=p.codigo_barras,
        created_at=p.created_at,
        owner=ProductOwner(user_id=p.user_id, email=emails.get(p.user_id)),
    )


@router.get("/filters/options")
def filter_options(
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    categorias_query = db.query(Product.categoria).distinct()
    if user_id:
        categorias_query = categorias_query.filter(Product.user_id == user_id)
    categorias = [row[0] for row in categorias_query.all() if row[0]]

    owner_ids = [row[0] for row in db.query(Product.user_id).distinct().all()]
    emails = _email_map(db)
    owners = sorted(
        ({"user_id": uid, "email": emails.get(uid)} for uid in owner_ids),
        key=lambda o: (o["email"] or o["user_id"]).lower(),
    )
    return {"categorias": sorted(categorias), "owners": owners}


@router.get("/", response_model=PaginatedProducts)
def list_products(
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    only_with_image: bool = Query(False),
    vencido: Optional[bool] = Query(None),
    bajo_stock: bool = Query(False),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = _apply_filters(db.query(Product), search, categoria, user_id, only_with_image, vencido, bajo_stock)

    total = query.count()
    if sort_by:
        column = SORTABLE_FIELDS[sort_by]
        query = query.order_by(column.desc() if sort_dir == "desc" else column.asc())
    else:
        query = query.order_by(Product.created_at.desc())
    products = query.offset(offset).limit(limit).all()
    emails = _email_map(db)
    return PaginatedProducts(items=[_to_response(p, emails) for p in products], total=total)


@router.get("/export")
def export_products(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    only_with_image: bool = Query(False),
    vencido: Optional[bool] = Query(None),
    bajo_stock: bool = Query(False),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = _apply_filters(db.query(Product), search, categoria, user_id, only_with_image, vencido, bajo_stock)
    if sort_by:
        column = SORTABLE_FIELDS[sort_by]
        query = query.order_by(column.desc() if sort_dir == "desc" else column.asc())
    else:
        query = query.order_by(Product.created_at.desc())
    products = query.all()
    emails = _email_map(db)

    if format == "xlsx":
        content = products_to_xlsx(products, owner_emails=emails)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "productos.xlsx"
    else:
        content = products_to_csv(products, owner_emails=emails)
        media_type = "text/csv"
        filename = "productos.csv"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    current_admin: AdminUser = Depends(get_current_admin),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")

    owner_email = _email_map(db).get(p.user_id)
    db.add(
        AdminActivityLog(
            event_type="product_deleted",
            actor_email=current_admin.email,
            target_user_id=p.user_id,
            target_email=owner_email,
            description=f"{current_admin.email} eliminó el producto \"{p.nombre}\" de {owner_email or p.user_id}",
        )
    )
    # Sin ON DELETE CASCADE en alerts.product_id, Postgres rechaza el borrado si el producto tiene alertas.
    db.query(Alert).filter(Alert.product_id == p.id).delete(synchronize_session=False)
    db.delete(p)
    db.commit()
