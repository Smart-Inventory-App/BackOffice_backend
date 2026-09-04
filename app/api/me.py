import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.export import products_to_csv, products_to_xlsx
from app.core.security import get_current_user
from app.models.app_models import (
    DEFAULT_DIAS_ALERTA_VENCIMIENTO,
    DEFAULT_STOCK_MINIMO,
    AuthUser,
    ConsumptionEvent,
    Product,
    UserProfile,
)
from app.models.audit import AdminActivityLog
from app.schemas.platform import (
    BulkCommitRequest,
    BulkCommitResult,
    BulkParsedRow,
    BulkParseResponse,
    BulkProductRow,
    CategoryCount,
    ConsumptionEventOut,
    EventCorrection,
    MyBulkEditRequest,
    MyBulkEditResult,
    MyDefaults,
    MyProductCreate,
    MyProductResponse,
    MyProductUpdate,
    MyProfileResponse,
    MyProfileUpdate,
    MyStats,
    PaginatedMyProducts,
    ProductSuggestion,
    SalidaRequest,
)

router = APIRouter(prefix="/me", tags=["me"])

REQUIRED_FIELDS = ("nombre", "categoria", "stock")

MY_SORTABLE_FIELDS = {
    "nombre": Product.nombre,
    "stock": Product.stock,
    "vencimiento": Product.vencimiento,
    "categoria": Product.categoria,
}


def _log_activity(
    db: Session,
    event_type: str,
    actor_email: Optional[str],
    target_user_id: str,
    target_email: Optional[str],
    description: str,
) -> None:
    db.add(
        AdminActivityLog(
            event_type=event_type,
            actor_email=actor_email,
            target_user_id=target_user_id,
            target_email=target_email,
            description=description,
        )
    )
    db.commit()


def _log_consumption(
    db: Session,
    product_id: int,
    user_id: str,
    quantity: int,
    event_type: str,
    actor_email: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    if quantity <= 0:
        return
    db.add(
        ConsumptionEvent(
            product_id=product_id,
            user_id=user_id,
            quantity=quantity,
            event_type=event_type,
            actor_email=actor_email,
            note=note,
        )
    )
    db.commit()


def _get_own_product(product_id: int, user_id: str, db: Session) -> Product:
    product = db.query(Product).filter(Product.id == product_id, Product.user_id == user_id).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    return product


def _get_or_create_profile(user_id: str, db: Session) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        profile = UserProfile(user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _passes_alert_filters(
    p: Product, profile: UserProfile, bajo_stock: bool, por_vencer: bool, today: date
) -> bool:
    """Cada producto puede tener su propia regla de stock mínimo / días de
    alerta de vencimiento; si no la tiene, se usa la del perfil del usuario."""
    if bajo_stock:
        threshold = p.stock_minimo or profile.stock_minimo or DEFAULT_STOCK_MINIMO
        if not (p.stock < threshold):
            return False
    if por_vencer:
        dias = p.dias_alerta_vencimiento or profile.dias_alerta_vencimiento or DEFAULT_DIAS_ALERTA_VENCIMIENTO
        if not (p.vencimiento and today <= p.vencimiento <= today + timedelta(days=dias)):
            return False
    return True


def _find_matching_product(
    user_id: str, nombre: str, marca: Optional[str], vencimiento: Optional[date], db: Session
) -> Optional[Product]:
    query = db.query(Product).filter(
        Product.user_id == user_id,
        func.lower(func.trim(Product.nombre)) == nombre.strip().lower(),
    )
    if vencimiento is None:
        query = query.filter(Product.vencimiento.is_(None))
    else:
        query = query.filter(Product.vencimiento == vencimiento)
    marca_norm = marca.strip().lower() if marca else None
    if marca_norm is None:
        query = query.filter(Product.marca.is_(None))
    else:
        query = query.filter(func.lower(func.trim(Product.marca)) == marca_norm)
    return query.first()


@router.get("/stats", response_model=MyStats)
def my_stats(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    today = date.today()
    profile = _get_or_create_profile(uid, db)
    own = db.query(Product).filter(Product.user_id == uid)

    total_products = own.count()
    products_with_image = own.filter(Product.imagen_url.isnot(None), Product.imagen_url != "").count()
    products_all = own.all()
    low_stock = sum(1 for p in products_all if _passes_alert_filters(p, profile, True, False, today))
    expiring_soon = sum(1 for p in products_all if _passes_alert_filters(p, profile, False, True, today))
    expired = own.filter(Product.vencimiento < today).count()

    by_category = [
        CategoryCount(label=row[0] or "Sin categoría", count=row[1])
        for row in db.query(Product.categoria, func.count(Product.id))
        .filter(Product.user_id == uid)
        .group_by(Product.categoria)
        .order_by(func.count(Product.id).desc())
        .all()
    ]

    return MyStats(
        total_products=total_products,
        products_with_image=products_with_image,
        low_stock_products=low_stock,
        expiring_soon_products=expiring_soon,
        expired_products=expired,
        products_by_category=by_category,
    )


@router.get("/products", response_model=PaginatedMyProducts)
def my_products(
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    vencido: Optional[bool] = Query(None),
    por_vencer: bool = Query(False),
    bajo_stock: bool = Query(False),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    query = db.query(Product).filter(Product.user_id == uid)
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))
    if vencido is True:
        query = query.filter(Product.vencimiento.isnot(None), Product.vencimiento < date.today())
    elif vencido is False:
        query = query.filter(
            (Product.vencimiento.is_(None)) | (Product.vencimiento >= date.today())
        )

    if bajo_stock or por_vencer:
        # bajo_stock/por_vencer dependen de la regla de cada producto (o la del
        # perfil si el producto no tiene una propia), así que no se puede
        # resolver con un único WHERE — se filtra en Python.
        profile = _get_or_create_profile(uid, db)
        today = date.today()
        candidates = [p for p in query.all() if _passes_alert_filters(p, profile, bajo_stock, por_vencer, today)]
        total = len(candidates)
        if sort_by == "nombre":
            candidates.sort(key=lambda p: (p.nombre or "").lower(), reverse=(sort_dir == "desc"))
        elif sort_by == "stock":
            candidates.sort(key=lambda p: p.stock, reverse=(sort_dir == "desc"))
        elif sort_by == "vencimiento":
            candidates.sort(key=lambda p: p.vencimiento or date.max, reverse=(sort_dir == "desc"))
        elif sort_by == "categoria":
            candidates.sort(key=lambda p: (p.categoria or "").lower(), reverse=(sort_dir == "desc"))
        else:
            candidates.sort(key=lambda p: p.created_at or datetime.min, reverse=True)
        products = candidates[offset : offset + limit]
        return PaginatedMyProducts(items=products, total=total)

    total = query.count()
    if sort_by:
        column = MY_SORTABLE_FIELDS[sort_by]
        query = query.order_by(column.desc() if sort_dir == "desc" else column.asc())
    else:
        query = query.order_by(Product.created_at.desc())
    products = query.offset(offset).limit(limit).all()
    return PaginatedMyProducts(items=products, total=total)


@router.get("/products/export")
def export_my_products(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    vencido: Optional[bool] = Query(None),
    por_vencer: bool = Query(False),
    bajo_stock: bool = Query(False),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    query = db.query(Product).filter(Product.user_id == uid)
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))
    if vencido is True:
        query = query.filter(Product.vencimiento.isnot(None), Product.vencimiento < date.today())
    elif vencido is False:
        query = query.filter(
            (Product.vencimiento.is_(None)) | (Product.vencimiento >= date.today())
        )
    if sort_by:
        column = MY_SORTABLE_FIELDS[sort_by]
        query = query.order_by(column.desc() if sort_dir == "desc" else column.asc())
    else:
        query = query.order_by(Product.created_at.desc())
    products = query.all()

    if bajo_stock or por_vencer:
        profile = _get_or_create_profile(uid, db)
        today = date.today()
        products = [p for p in products if _passes_alert_filters(p, profile, bajo_stock, por_vencer, today)]

    if format == "xlsx":
        content = products_to_xlsx(products)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "mis_productos.xlsx"
    else:
        content = products_to_csv(products)
        media_type = "text/csv"
        filename = "mis_productos.csv"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/products/filters/options")
def my_filter_options(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    base = db.query(Product).filter(Product.user_id == str(current.id))
    categorias = [row[0] for row in base.with_entities(Product.categoria).distinct().all() if row[0]]
    ubicaciones = [row[0] for row in base.with_entities(Product.ubicacion).distinct().all() if row[0]]
    marcas = [row[0] for row in base.with_entities(Product.marca).distinct().all() if row[0]]
    return {"categorias": sorted(categorias), "ubicaciones": sorted(ubicaciones), "marcas": sorted(marcas)}


@router.get("/profile", response_model=MyProfileResponse)
def my_profile(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    profile = _get_or_create_profile(str(current.id), db)
    return MyProfileResponse(
        email=current.email,
        first_name=profile.first_name,
        last_name=profile.last_name,
        username=profile.username,
        stock_minimo=profile.stock_minimo or DEFAULT_STOCK_MINIMO,
        dias_alerta_vencimiento=profile.dias_alerta_vencimiento or DEFAULT_DIAS_ALERTA_VENCIMIENTO,
    )


@router.put("/profile", response_model=MyProfileResponse)
def update_my_profile(
    data: MyProfileUpdate,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    profile = _get_or_create_profile(str(current.id), db)
    if data.first_name is not None:
        profile.first_name = data.first_name
    if data.last_name is not None:
        profile.last_name = data.last_name
    if data.username is not None:
        profile.username = data.username
    if data.stock_minimo is not None:
        profile.stock_minimo = data.stock_minimo
    if data.dias_alerta_vencimiento is not None:
        profile.dias_alerta_vencimiento = data.dias_alerta_vencimiento
    db.commit()
    db.refresh(profile)
    return MyProfileResponse(
        email=current.email,
        first_name=profile.first_name,
        last_name=profile.last_name,
        username=profile.username,
        stock_minimo=profile.stock_minimo or DEFAULT_STOCK_MINIMO,
        dias_alerta_vencimiento=profile.dias_alerta_vencimiento or DEFAULT_DIAS_ALERTA_VENCIMIENTO,
    )


@router.get("/products/suggest", response_model=list[ProductSuggestion])
def suggest_products(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    rows = (
        db.query(Product)
        .filter(Product.user_id == str(current.id), Product.nombre.ilike(f"%{q}%"))
        .order_by(Product.nombre.asc(), Product.vencimiento.asc())
        .limit(10)
        .all()
    )
    return [
        ProductSuggestion(
            id=p.id,
            nombre=p.nombre,
            marca=p.marca,
            categoria=p.categoria,
            ubicacion=p.ubicacion,
            vencimiento=p.vencimiento,
            stock=p.stock,
            stock_minimo=p.stock_minimo,
            dias_alerta_vencimiento=p.dias_alerta_vencimiento,
        )
        for p in rows
    ]


@router.get("/products/defaults", response_model=MyDefaults)
def my_defaults(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    profile = db.query(UserProfile).filter(UserProfile.user_id == str(current.id)).first()
    if not profile:
        return MyDefaults()
    return MyDefaults(last_categoria=profile.last_categoria, last_ubicacion=profile.last_ubicacion)


@router.post("/products", response_model=MyProductResponse, status_code=201)
def create_my_product(
    data: MyProductCreate,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)

    profile = _get_or_create_profile(uid, db)
    profile.last_categoria = data.categoria
    profile.last_ubicacion = data.ubicacion
    db.commit()

    existing = _find_matching_product(uid, data.nombre, data.marca, data.vencimiento, db)
    if existing:
        existing.stock += data.stock
        db.commit()
        db.refresh(existing)
        _log_consumption(db, existing.id, uid, data.stock, "restocked", actor_email=current.email)
        _log_activity(
            db, "product_updated", current.email, uid, current.email,
            f"{current.email} sumó {data.stock} unidades a \"{existing.nombre}\" (stock: {existing.stock}) desde el backoffice",
        )
        return MyProductResponse.model_validate(existing, from_attributes=True).model_copy(update={"merged": True})

    product = Product(user_id=uid, **data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    _log_activity(
        db, "product_created", current.email, uid, current.email,
        f"{current.email} cargó \"{product.nombre}\" (stock: {product.stock}) desde el backoffice",
    )
    return product


@router.patch("/products/bulk-edit", response_model=MyBulkEditResult)
def bulk_edit_my_products(
    data: MyBulkEditRequest,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    if not data.product_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se seleccionó ningún producto")

    changes: dict = {}
    if data.categoria is not None:
        changes["categoria"] = data.categoria
    if data.ubicacion is not None:
        changes["ubicacion"] = data.ubicacion
    if data.marca is not None:
        changes["marca"] = data.marca
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se especificó ningún cambio")

    uid = str(current.id)
    products = (
        db.query(Product)
        .filter(Product.id.in_(data.product_ids), Product.user_id == uid)
        .all()
    )
    if not products:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontraron productos")

    for p in products:
        for field, value in changes.items():
            setattr(p, field, value)
    db.commit()

    campos = ", ".join(changes.keys())
    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} editó {len(products)} productos en lote ({campos}) desde el backoffice",
    )
    return MyBulkEditResult(updated=len(products))


@router.patch("/products/{product_id}", response_model=MyProductResponse)
def update_my_product(
    product_id: int,
    data: MyProductUpdate,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    product = _get_own_product(product_id, str(current.id), db)
    update_dict = data.model_dump(exclude_unset=True)
    new_stock = update_dict.get("stock")
    old_stock = product.stock

    changed_fields = list(update_dict.keys())
    for field, value in update_dict.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)

    if new_stock is not None and new_stock != old_stock:
        if new_stock < old_stock:
            _log_consumption(db, product.id, str(current.id), old_stock - new_stock, "consumed", actor_email=current.email)
        else:
            _log_consumption(db, product.id, str(current.id), new_stock - old_stock, "restocked", actor_email=current.email)

    if changed_fields:
        _log_activity(
            db, "product_updated", current.email, str(current.id), current.email,
            f"{current.email} editó \"{product.nombre}\" ({', '.join(changed_fields)}) desde el backoffice",
        )
    return product


@router.post("/products/salida", response_model=MyProductResponse)
def register_salida(
    data: SalidaRequest,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    product = _get_own_product(data.product_id, uid, db)
    if data.quantity > product.stock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No hay suficiente stock: tenés {product.stock} y pediste sacar {data.quantity}",
        )

    product.stock -= data.quantity
    db.commit()
    db.refresh(product)

    _log_consumption(db, product.id, uid, data.quantity, data.event_type, actor_email=current.email, note=data.note)
    motivo = "vencido" if data.event_type == "expired" else "consumido"
    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} sacó {data.quantity} unidades de \"{product.nombre}\" ({motivo}, stock: {product.stock}) desde el backoffice",
    )
    return product


def _stock_effect(event_type: str, quantity: int) -> int:
    return quantity if event_type == "restocked" else -quantity


def _get_own_consumption_event(product_id: int, event_id: int, db: Session) -> ConsumptionEvent:
    event = db.query(ConsumptionEvent).filter(
        ConsumptionEvent.id == event_id,
        ConsumptionEvent.product_id == product_id,
    ).first()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Registro de consumo no encontrado")
    if event.reverses_event_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este registro ya es una corrección — no se puede corregir de nuevo",
        )
    already_reversed = db.query(ConsumptionEvent).filter(
        ConsumptionEvent.reverses_event_id == event.id
    ).first()
    if already_reversed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este registro ya fue corregido — corregí la versión más reciente",
        )
    return event


@router.get("/products/{product_id}/consumption", response_model=list[ConsumptionEventOut])
def get_consumption_history(
    product_id: int,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    _get_own_product(product_id, uid, db)
    events = (
        db.query(ConsumptionEvent)
        .filter(ConsumptionEvent.product_id == product_id)
        .order_by(ConsumptionEvent.consumed_at.asc())
        .all()
    )
    reversed_ids = {e.reverses_event_id for e in events if e.reverses_event_id is not None}
    return [
        ConsumptionEventOut(
            id=e.id,
            quantity=e.quantity,
            event_type=e.event_type,
            consumed_at=e.consumed_at,
            note=e.note,
            actor_email=e.actor_email,
            reverses_event_id=e.reverses_event_id,
            reversed=e.id in reversed_ids,
        )
        for e in events
    ]


@router.put("/products/{product_id}/events/{event_id}")
def update_consumption_event(
    product_id: int,
    event_id: int,
    data: EventCorrection,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    product = _get_own_product(product_id, uid, db)
    event = _get_own_consumption_event(product_id, event_id, db)

    new_stock = (
        product.stock
        - _stock_effect(event.event_type, event.quantity)
        + _stock_effect(data.event_type, data.quantity)
    )
    if new_stock < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No se puede corregir: dejaría el stock en negativo",
        )

    reversal_type = "consumed" if event.event_type == "restocked" else "restocked"
    db.add(ConsumptionEvent(
        product_id=product.id,
        user_id=product.user_id,
        quantity=event.quantity,
        event_type=reversal_type,
        actor_email=current.email,
        note=f"Corrección: revierte el registro #{event.id}",
        reverses_event_id=event.id,
    ))
    new_event = ConsumptionEvent(
        product_id=product.id,
        user_id=product.user_id,
        quantity=data.quantity,
        event_type=data.event_type,
        actor_email=current.email,
        note=data.note or f"Corrige el registro #{event.id} (antes: {event.quantity} {event.event_type})",
    )
    db.add(new_event)
    product.stock = new_stock
    db.commit()
    db.refresh(product)
    db.refresh(new_event)

    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} corrigió un consumo de \"{product.nombre}\" "
        f"(de {event.quantity} a {data.quantity} unidades) desde el backoffice",
    )
    return {"event_type": new_event.event_type, "quantity": new_event.quantity, "remaining_stock": product.stock}


@router.delete("/products/{product_id}/events/{event_id}")
def delete_consumption_event(
    product_id: int,
    event_id: int,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    product = _get_own_product(product_id, uid, db)
    event = _get_own_consumption_event(product_id, event_id, db)

    quantity = event.quantity
    motivo = event.event_type
    new_stock = product.stock - _stock_effect(motivo, quantity)
    if new_stock < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No se puede borrar: dejaría el stock en negativo",
        )
    reversal_type = "consumed" if motivo == "restocked" else "restocked"
    db.add(ConsumptionEvent(
        product_id=product.id,
        user_id=product.user_id,
        quantity=quantity,
        event_type=reversal_type,
        actor_email=current.email,
        note=f"Deshecho: revierte el registro #{event.id}",
        reverses_event_id=event.id,
    ))
    product.stock = new_stock
    db.commit()
    db.refresh(product)

    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} deshizo un registro de consumo de \"{product.nombre}\" "
        f"({quantity} unidades, {motivo}) desde el backoffice",
    )
    return {"remaining_stock": product.stock}


# ---- Carga masiva (CSV / Excel) ----

def _coerce_stock(raw) -> tuple[Optional[int], Optional[str]]:
    if raw is None or str(raw).strip() == "":
        return None, "stock es obligatorio"
    try:
        value = int(float(str(raw).strip()))
    except ValueError:
        return None, "stock debe ser un número entero"
    if value <= 0:
        return None, "stock debe ser mayor a 0"
    return value, None


def _coerce_vencimiento(raw) -> tuple[Optional[date], Optional[str]]:
    if raw is None or str(raw).strip() == "":
        return None, None
    if isinstance(raw, datetime):
        return raw.date(), None
    if isinstance(raw, date):
        return raw, None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, "vencimiento debe tener formato AAAA-MM-DD"


def _clean_str(raw) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _coerce_optional_int(raw, field_label: str) -> tuple[Optional[int], Optional[str]]:
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        value = int(float(str(raw).strip()))
    except ValueError:
        return None, f"{field_label} debe ser un número entero"
    if value <= 0:
        return None, f"{field_label} debe ser mayor a 0"
    return value, None


def _validate_row(raw: dict, row_number: int, mode: str = "entrada") -> BulkParsedRow:
    errors: list[str] = []

    nombre = _clean_str(raw.get("nombre"))
    if not nombre:
        errors.append("nombre es obligatorio")

    categoria = _clean_str(raw.get("categoria"))
    if mode == "entrada" and not categoria:
        errors.append("categoria es obligatoria")

    stock, stock_error = _coerce_stock(raw.get("stock"))
    if stock_error:
        errors.append(stock_error)

    vencimiento, venc_error = _coerce_vencimiento(raw.get("vencimiento"))
    if venc_error:
        errors.append(venc_error)

    stock_minimo, stock_minimo_error = _coerce_optional_int(raw.get("stock_minimo"), "stock_minimo")
    if stock_minimo_error:
        errors.append(stock_minimo_error)

    dias_alerta, dias_alerta_error = _coerce_optional_int(
        raw.get("dias_alerta_vencimiento"), "dias_alerta_vencimiento"
    )
    if dias_alerta_error:
        errors.append(dias_alerta_error)

    # Siempre devolvemos los datos parseados (aunque haya errores) para que el
    # frontend pueda mostrarlos en un editor y el usuario los corrija a mano,
    # en vez de tener que volver a subir el archivo.
    data = BulkProductRow(
        row_number=row_number,
        nombre=nombre or "",
        marca=_clean_str(raw.get("marca")),
        categoria=categoria or "",
        stock=stock or 0,
        vencimiento=vencimiento,
        descripcion=_clean_str(raw.get("descripcion")),
        imagen_url=_clean_str(raw.get("imagen_url")),
        ubicacion=_clean_str(raw.get("ubicacion")),
        stock_minimo=stock_minimo,
        dias_alerta_vencimiento=dias_alerta,
    )
    return BulkParsedRow(row_number=row_number, data=data, valid=not errors, errors=errors)


def _read_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]


def _read_xlsx(content: bytes) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []
    headers = [str(h).strip().lower() if h is not None else "" for h in header_row]

    rows = []
    for row in rows_iter:
        if row is None or all(v is None for v in row):
            continue
        rows.append({headers[i]: row[i] for i in range(len(headers)) if i < len(row)})
    return rows


@router.post("/products/bulk/parse", response_model=BulkParseResponse)
def bulk_parse(
    mode: str = Query("entrada", pattern="^(entrada|salida)$"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    filename = (file.filename or "").lower()
    content = file.file.read()

    if filename.endswith(".csv"):
        raw_rows = _read_csv(content)
    elif filename.endswith(".xlsx"):
        raw_rows = _read_xlsx(content)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato de archivo no soportado: usá .csv o .xlsx",
        )

    uid = str(current.id)
    local_stock: dict[tuple[str, Optional[str], Optional[date]], int] = {}
    parsed: list[BulkParsedRow] = []

    for idx, raw in enumerate(raw_rows, start=2):  # fila 1 es el encabezado
        result = _validate_row(raw, idx, mode)
        if result.valid and result.data:
            marca_key = result.data.marca.strip().lower() if result.data.marca else None
            key = (result.data.nombre.strip().lower(), marca_key, result.data.vencimiento)

            if mode == "salida":
                if key in local_stock:
                    available = local_stock[key]
                else:
                    existing = _find_matching_product(
                        uid, result.data.nombre, result.data.marca, result.data.vencimiento, db
                    )
                    available = existing.stock if existing else None
                if available is None:
                    result.valid = False
                    result.errors.append("no existe un producto tuyo con ese nombre/marca/vencimiento")
                elif result.data.stock > available:
                    result.valid = False
                    result.errors.append(f"no hay suficiente stock disponible (hay {available})")
                else:
                    result.action = "salida"
                    result.existing_stock = available
                    local_stock[key] = available - result.data.stock
            elif key in local_stock:
                result.action = "merge"
                result.existing_stock = local_stock[key]
                local_stock[key] += result.data.stock
            else:
                existing = _find_matching_product(uid, result.data.nombre, result.data.marca, result.data.vencimiento, db)
                if existing:
                    result.action = "merge"
                    result.existing_stock = existing.stock
                    local_stock[key] = existing.stock + result.data.stock
                else:
                    result.action = "create"
                    local_stock[key] = result.data.stock
        parsed.append(result)

    return BulkParseResponse(rows=parsed)


@router.post("/products/bulk/commit", response_model=BulkCommitResult)
def bulk_commit(
    body: BulkCommitRequest,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    created = 0
    merged = 0
    failed = 0
    errors: list[str] = []

    for row in body.rows:
        try:
            if body.mode == "salida":
                existing = _find_matching_product(uid, row.nombre, row.marca, row.vencimiento, db)
                if not existing:
                    raise ValueError("no existe un producto tuyo con ese nombre/marca/vencimiento")
                if row.stock > existing.stock:
                    raise ValueError(f"no hay suficiente stock disponible (hay {existing.stock})")
                existing.stock -= row.stock
                db.commit()
                _log_consumption(db, existing.id, uid, row.stock, "consumed", actor_email=current.email, note="Carga masiva")
                merged += 1
                _log_activity(
                    db, "product_updated", current.email, uid, current.email,
                    f"{current.email} sacó {row.stock} unidades de \"{existing.nombre}\" (stock: {existing.stock}) por carga masiva",
                )
                continue

            existing = _find_matching_product(uid, row.nombre, row.marca, row.vencimiento, db)
            if existing:
                existing.stock += row.stock
                merged += 1
                nombre_log = existing.nombre
                stock_log = existing.stock
                kind_log = "sumó"
            else:
                product = Product(
                    user_id=uid,
                    nombre=row.nombre,
                    marca=row.marca,
                    categoria=row.categoria,
                    stock=row.stock,
                    vencimiento=row.vencimiento,
                    descripcion=row.descripcion,
                    imagen_url=row.imagen_url,
                    ubicacion=row.ubicacion,
                    stock_minimo=row.stock_minimo,
                    dias_alerta_vencimiento=row.dias_alerta_vencimiento,
                )
                db.add(product)
                created += 1
                nombre_log = row.nombre
                stock_log = row.stock
                kind_log = "cargó"
            db.commit()
            if existing:
                _log_consumption(db, existing.id, uid, row.stock, "restocked", actor_email=current.email, note="Carga masiva")
            _log_activity(
                db, "product_created" if kind_log == "cargó" else "product_updated",
                current.email, uid, current.email,
                f"{current.email} {kind_log} \"{nombre_log}\" (stock: {stock_log}) por carga masiva",
            )
        except Exception as e:
            db.rollback()
            failed += 1
            errors.append(f"Fila {row.row_number}: {e}")

    return BulkCommitResult(created=created, merged=merged, failed=failed, errors=errors)
