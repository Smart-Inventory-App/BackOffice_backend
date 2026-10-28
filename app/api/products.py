from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session, Query as SAQuery

from app.core import alerts_view
from app.core.argentina import ar_today
from app.core.database import get_db
from app.core.export import products_to_csv, products_to_xlsx
from app.core.security import get_current_admin
from app.core.sorting import alphabetical_key, only_sortable, page_of_products
from app.models.admin import AdminUser
from app.models.app_models import Alert, AuthUser, Product
from app.models.audit import AdminActivityLog
from app.schemas.platform import (
    AdminProductGroupDetail,
    AdminProductGroupSummary,
    AdminProductResponse,
    PaginatedProducts,
    ProductOwner,
)

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
    db: Session,
    query: SAQuery,
    search: Optional[str],
    categoria: Optional[str],
    user_id: Optional[str],
    only_with_image: bool,
    vencido: Optional[bool],
    bajo_stock: bool,
    alert_level: Optional[str] = None,
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
    today = ar_today()
    if vencido is True:
        query = query.filter(Product.vencimiento.isnot(None), Product.vencimiento < today)
    elif vencido is False:
        query = query.filter((Product.vencimiento.is_(None)) | (Product.vencimiento >= today))
    if bajo_stock or alert_level:
        # Las alertas dependen de la regla de cada producto (o la del perfil de su dueño) y del modelo predictivo, así
        # que no se resuelven con un único WHERE: se buscan con el mismo criterio que el Centro de Alertas.
        alerts = alerts_view.alerts_for(db, user_id or None)
        if bajo_stock:
            alerts = [a for a in alerts if a.type == alerts_view.STOCK_TYPE]
        if alert_level:
            alerts = [a for a in alerts if a.level == alert_level]
        query = query.filter(Product.id.in_({a.product_id for a in alerts}))
    return query


def _ordered(query: SAQuery, sort_by: Optional[str], sort_dir: str, offset: int = 0, limit: Optional[int] = None):
    return page_of_products(
        query,
        sort_by,
        sort_dir,
        SORTABLE_FIELDS,
        (Product.created_at.desc(), Product.id.desc()),
        Product.id,
        lambda product: product.nombre,
        offset,
        limit,
    )


def _to_response(p: Product, emails: dict[str, str], levels: dict | None = None) -> AdminProductResponse:
    level, types = (levels or {}).get(p.id, (None, []))
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
        stock_minimo=p.stock_minimo,
        dias_alerta_vencimiento=p.dias_alerta_vencimiento,
        created_at=p.created_at,
        alert_level=level,
        alert_types=types,
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
    alert_level: Optional[str] = Query(None, pattern="^(critical|warning)$"),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = _apply_filters(db, db.query(Product), search, categoria, user_id, only_with_image, vencido, bajo_stock, alert_level)
    query = only_sortable(query, sort_by, Product.vencimiento)

    total = query.count()
    products = _ordered(query, sort_by, sort_dir, offset, limit)
    emails = _email_map(db)
    levels = alerts_view.levels_by_product(db, [p.id for p in products])
    return PaginatedProducts(items=[_to_response(p, emails, levels) for p in products], total=total)


@router.get("/export")
def export_products(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    only_with_image: bool = Query(False),
    vencido: Optional[bool] = Query(None),
    bajo_stock: bool = Query(False),
    alert_level: Optional[str] = Query(None, pattern="^(critical|warning)$"),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = _apply_filters(db, db.query(Product), search, categoria, user_id, only_with_image, vencido, bajo_stock, alert_level)
    query = only_sortable(query, sort_by, Product.vencimiento)
    products = _ordered(query, sort_by, sort_dir)
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


# ── stock total de un mismo producto (mismo nombre y categoría) ───────────────

def _group_key(nombre: str, categoria: str) -> tuple[str, str]:
    """Qué cuenta como "el mismo producto" para el stock total: igual nombre y categoría, sin distinguir
    mayúsculas, tildes ni espacios sobrantes. La marca no entra en la clave, ni nada de lo que cambia entre lotes."""

    def norm(text: Optional[str]) -> str:
        return alphabetical_key(" ".join((text or "").split()))

    return (norm(nombre), norm(categoria))


def _groups(db: Session, user_id: Optional[str]) -> dict[tuple[str, str], list[Product]]:
    """Productos agrupados por nombre y categoría: los de un usuario, o los de toda la plataforma si no se indica."""
    query = db.query(Product)
    if user_id:
        query = query.filter(Product.user_id == user_id)
    groups: dict[tuple[str, str], list[Product]] = {}
    for product in query.order_by(Product.id.asc()).all():
        groups.setdefault(_group_key(product.nombre, product.categoria), []).append(product)
    return groups


def _summarize(lots: list[Product]) -> AdminProductGroupSummary:
    first = lots[0]
    expiries = [p.vencimiento for p in lots if p.stock > 0 and p.vencimiento]
    return AdminProductGroupSummary(
        nombre=first.nombre,
        categoria=first.categoria,
        stock_total=sum(p.stock for p in lots),
        lotes=len(lots),
        usuarios=len({p.user_id for p in lots}),
        proximo_vencimiento=min(expiries) if expiries else None,
    )


@router.get("/groups", response_model=list[AdminProductGroupSummary])
def search_product_groups(
    q: str = Query(..., min_length=1),
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """Productos agrupados por nombre y categoría cuyo nombre contiene `q` (sin distinguir mayúsculas ni tildes).

    Con `user_id` se cuenta solo lo de ese usuario; sin él, lo de todos los usuarios de la plataforma.
    """
    needle = alphabetical_key(" ".join(q.split()))
    if not needle:
        return []
    found = [_summarize(lots) for key, lots in _groups(db, user_id).items() if needle in key[0]]
    found.sort(key=lambda g: (alphabetical_key(g.nombre), alphabetical_key(g.categoria)))
    return found[:10]


@router.get("/group", response_model=AdminProductGroupDetail)
def get_product_group(
    nombre: str = Query(..., min_length=1),
    categoria: str = Query(..., min_length=1),
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    """Todos los productos con ese nombre y categoría (de cualquier marca) y su stock total.

    Salen ordenados por vencimiento (los que no vencen, al final).
    """
    lots = _groups(db, user_id).get(_group_key(nombre, categoria))
    if not lots:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    emails = _email_map(db)
    ordered = sorted(lots, key=lambda p: (p.vencimiento is None, p.vencimiento or date.max, p.id))
    return AdminProductGroupDetail(
        **_summarize(lots).model_dump(),
        productos=[_to_response(p, emails, alerts_view.levels_by_product(db, [p.id for p in ordered])) for p in ordered],
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
    return _to_response(p, _email_map(db), alerts_view.levels_by_product(db, [p.id]))


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
