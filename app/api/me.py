import csv
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import alerts_view, barcode, matching
from app.core.app_backend import refresh_user_alerts
from app.core.argentina import ar_hour_to_utc, ar_today, utc_hour_to_ar
from app.core.database import get_db
from app.core.export import products_to_csv, products_to_xlsx
from app.core.security import get_current_user
from app.core.sorting import alphabetical_key, only_sortable, page_of_products
from app.api import products as admin_products
from app.models.app_models import (
    DEFAULT_DIAS_ALERTA_VENCIMIENTO,
    DEFAULT_STOCK_MINIMO,
    DEFAULT_ALERT_CHECK_HOUR,
    DEFAULT_USER_TYPE,
    Alert,
    AuthUser,
    ConsumptionEvent,
    Product,
    UserProfile,
)
from app.models.audit import AdminActivityLog
from app.schemas.platform import (
    AdminProductGroupDetail,
    AdminProductGroupSummary,
    BulkCommitRequest,
    BulkCommitResult,
    BulkParsedRow,
    BulkParseResponse,
    BulkProductRow,
    CategoryCount,
    ConsumptionEventOut,
    EventCorrection,
    MAX_CATEGORIA,
    MAX_DESCRIPCION,
    MAX_NOMBRE,
    MyBulkDeleteRequest,
    MyBulkDeleteResult,
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
    PROFILE_TEXT_MAX,
    ProductSuggestion,
    SalidaRequest,
    StockChangeOut,
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


# Nota de los movimientos que salen de editar el stock de un producto (la app los muestra como «modificación»)
STOCK_EDIT_NOTE = "Modificación de stock"


# Cuándo vencen los productos que todavía tienen stock: «Vencidos», los de la semana, el mes, el trimestre, más lejos y los
# que no vencen. Es lo que dibuja el gráfico «Vencimientos» del dashboard.
EXPIRY_BUCKETS = (
    ("Vencidos", None, -1),
    ("0 a 7 días", 0, 7),
    ("8 a 30 días", 8, 30),
    ("31 a 90 días", 31, 90),
    ("Más de 90 días", 91, None),
)


def _expiry_buckets(products: list, today: date) -> list[CategoryCount]:
    counts = {label: 0 for label, _, _ in EXPIRY_BUCKETS}
    counts["Sin vencimiento"] = 0
    for product in products:
        if product.vencimiento is None:
            counts["Sin vencimiento"] += 1
            continue
        days = (product.vencimiento - today).days
        for label, low, high in EXPIRY_BUCKETS:
            if (low is None or days >= low) and (high is None or days <= high):
                counts[label] += 1
                break
    return [CategoryCount(label=label, count=count) for label, count in counts.items()]


def _alert_filter_ids(db: Session, user_id: str, bajo_stock: bool, por_vencer: bool) -> Optional[set]:
    """Ids de los productos con una alerta de stock bajo y/o de próximo vencimiento, con las reglas vigentes (la del
    producto o, si no tiene, la del perfil) y el mismo criterio que el Centro de Alertas. `None` si no se filtra."""
    if not (bajo_stock or por_vencer):
        return None
    alerts = alerts_view.alerts_for(db, user_id)
    ids = None
    if bajo_stock:
        ids = {a.product_id for a in alerts if a.type == alerts_view.STOCK_TYPE}
    if por_vencer:
        expiring = {a.product_id for a in alerts if a.type == "expiry" and a.days_to_expiry >= 0}
        ids = expiring if ids is None else ids & expiring
    return ids


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
    today = ar_today()
    own = db.query(Product).filter(Product.user_id == uid)

    total_products = own.count()
    total_units = db.query(func.coalesce(func.sum(Product.stock), 0)).filter(Product.user_id == uid).scalar()
    products_with_image = own.filter(Product.imagen_url.isnot(None), Product.imagen_url != "").count()
    summary = alerts_view.summarize(alerts_view.alerts_for(db, uid, today))
    expired = own.filter(Product.vencimiento < today, Product.stock > 0).count()

    by_category = [
        CategoryCount(label=row[0] or "Sin categoría", count=row[1])
        for row in db.query(Product.categoria, func.count(Product.id))
        .filter(Product.user_id == uid)
        .group_by(Product.categoria)
        .order_by(func.count(Product.id).desc())
        .all()
    ]

    stocked = own.filter(Product.stock > 0).all()
    by_stock: dict[str, int] = {}
    by_location: dict[str, int] = {}
    by_brand: dict[str, int] = {}
    for product in own.all():
        # Solo cuentan las ubicaciones y marcas que existen de verdad: «sin ubicación» o sin marca no es un lugar ni una marca
        location = (product.ubicacion or "").strip()
        if location:
            by_location[location] = by_location.get(location, 0) + 1
        brand = (product.marca or "").strip()
        if brand:
            by_brand[brand] = by_brand.get(brand, 0) + (product.stock or 0)
        by_stock[product.categoria or "Sin categoría"] = by_stock.get(product.categoria or "Sin categoría", 0) + product.stock

    return MyStats(
        total_products=total_products,
        stock_by_category=[CategoryCount(label=k, count=v) for k, v in sorted(by_stock.items(), key=lambda kv: -kv[1])],
        products_by_location=[CategoryCount(label=k, count=v) for k, v in sorted(by_location.items(), key=lambda kv: -kv[1])],
        stock_by_brand=[CategoryCount(label=k, count=v) for k, v in sorted(by_brand.items(), key=lambda kv: (-kv[1], kv[0]))],
        expiry_buckets=_expiry_buckets(stocked, today),
        total_units=int(total_units or 0),
        products_with_image=products_with_image,
        low_stock_products=summary.low_stock,
        expiring_soon_products=summary.expiring,
        expired_products=expired,
        critical_alerts=summary.critical,
        warning_alerts=summary.warning,
        products_by_category=by_category,
    )


def _filtered_products(db: Session, uid: str, search, categoria, ubicacion, vencido, marca=None):
    """Los productos del usuario que cumplen la búsqueda y los filtros que se resuelven con un WHERE."""
    query = db.query(Product).filter(Product.user_id == uid)
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))
    if ubicacion:
        query = query.filter(Product.ubicacion.ilike(ubicacion))
    if marca:
        query = query.filter(Product.marca.ilike(marca))
    today = ar_today()
    if vencido is True:
        query = query.filter(Product.vencimiento.isnot(None), Product.vencimiento < today)
    elif vencido is False:
        query = query.filter((Product.vencimiento.is_(None)) | (Product.vencimiento >= today))
    return query


@router.get("/products", response_model=PaginatedMyProducts)
def my_products(
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    ubicacion: Optional[str] = Query(None),
    marca: Optional[str] = Query(None),
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
    query = _filtered_products(db, uid, search, categoria, ubicacion, vencido, marca)

    alert_ids = _alert_filter_ids(db, uid, bajo_stock, por_vencer)
    if alert_ids is not None:
        # bajo_stock/por_vencer dependen de la regla de cada producto (o la del perfil si el producto no tiene una
        # propia) y del modelo predictivo, así que no se pueden resolver con un único WHERE — se filtra en Python.
        candidates = [p for p in query.all() if p.id in alert_ids]
        total = len(candidates)
        if sort_by == "vencimiento":
            # Los productos sin fecha de vencimiento no entran en este orden
            candidates = [p for p in candidates if p.vencimiento]
        total = len(candidates)
        reverse = sort_dir == "desc"
        if sort_by == "nombre":
            candidates.sort(key=lambda p: (alphabetical_key(p.nombre), p.nombre or "", p.id), reverse=reverse)
        elif sort_by == "stock":
            candidates.sort(key=lambda p: (p.stock, p.id), reverse=reverse)
        elif sort_by == "vencimiento":
            candidates.sort(key=lambda p: (p.vencimiento, p.id), reverse=reverse)
        elif sort_by == "categoria":
            candidates.sort(key=lambda p: (alphabetical_key(p.categoria), p.id), reverse=reverse)
        else:
            candidates.sort(key=lambda p: (p.created_at or datetime.min, p.id), reverse=True)
        products = candidates[offset : offset + limit]
        return PaginatedMyProducts(items=products, total=total)

    query = only_sortable(query, sort_by, Product.vencimiento)
    total = query.count()
    products = page_of_products(
        query, sort_by, sort_dir, MY_SORTABLE_FIELDS,
        (Product.created_at.desc(), Product.id.desc()), Product.id, lambda p: p.nombre, offset, limit,
    )
    return PaginatedMyProducts(items=products, total=total)


@router.get("/products/export")
def export_my_products(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    ubicacion: Optional[str] = Query(None),
    marca: Optional[str] = Query(None),
    vencido: Optional[bool] = Query(None),
    por_vencer: bool = Query(False),
    bajo_stock: bool = Query(False),
    sort_by: Optional[str] = Query(None, pattern="^(nombre|stock|vencimiento|categoria)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    query = _filtered_products(db, uid, search, categoria, ubicacion, vencido, marca)
    query = only_sortable(query, sort_by, Product.vencimiento)
    products = page_of_products(
        query, sort_by, sort_dir, MY_SORTABLE_FIELDS,
        (Product.created_at.desc(), Product.id.desc()), Product.id, lambda p: p.nombre,
    )

    alert_ids = _alert_filter_ids(db, uid, bajo_stock, por_vencer)
    if alert_ids is not None:
        products = [p for p in products if p.id in alert_ids]

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


@router.get("/products/stock-total")
def my_stock_total(
    search: Optional[str] = Query(None),
    categoria: Optional[str] = Query(None),
    ubicacion: Optional[str] = Query(None),
    marca: Optional[str] = Query(None),
    vencido: Optional[bool] = Query(None),
    por_vencer: bool = Query(False),
    bajo_stock: bool = Query(False),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Unidades en stock y cantidad de productos de la lista, con la misma búsqueda y los mismos filtros que el inventario."""
    uid = str(current.id)
    products = _filtered_products(db, uid, search, categoria, ubicacion, vencido, marca).all()
    alert_ids = _alert_filter_ids(db, uid, bajo_stock, por_vencer)
    if alert_ids is not None:
        products = [p for p in products if p.id in alert_ids]
    return {"stock_total": sum(p.stock or 0 for p in products), "products": len(products)}


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


def _clean_username(username: Optional[str]) -> Optional[str]:
    """El nombre de usuario sin espacios en los bordes; vacío es lo mismo que no tener."""
    cleaned = (username or "").strip()
    return cleaned or None


def _same_username(a: Optional[str], b: Optional[str]) -> bool:
    """Dos nombres son el mismo aunque cambien las mayúsculas: «Ana» y «ana» no pueden convivir."""
    return a is not None and b is not None and a.lower() == b.lower()


def _username_taken(username: str, user_id: str, db: Session) -> bool:
    """Si otra cuenta ya tiene ese nombre de usuario."""
    return (
        db.query(UserProfile.user_id)
        .filter(func.lower(UserProfile.username) == username.lower(), UserProfile.user_id != user_id)
        .first()
        is not None
    )


def _profile_response(email: str, profile: UserProfile) -> MyProfileResponse:
    check_hour = profile.alert_check_hour if profile.alert_check_hour is not None else DEFAULT_ALERT_CHECK_HOUR
    return MyProfileResponse(
        email=email,
        first_name=profile.first_name,
        last_name=profile.last_name,
        username=profile.username,
        stock_minimo=profile.stock_minimo or DEFAULT_STOCK_MINIMO,
        dias_alerta_vencimiento=profile.dias_alerta_vencimiento or DEFAULT_DIAS_ALERTA_VENCIMIENTO,
        alert_check_hour=utc_hour_to_ar(check_hour),
        user_type=profile.user_type or DEFAULT_USER_TYPE,
    )


@router.get("/profile", response_model=MyProfileResponse)
def my_profile(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    return _profile_response(current.email, _get_or_create_profile(str(current.id), db))


@router.get("/profile/username-available")
def username_available(
    username: str = Query(..., max_length=PROFILE_TEXT_MAX),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Si el nombre de usuario se puede usar. Un nombre que ya es el de esta cuenta cuenta como disponible."""
    uid = str(current.id)
    cleaned = _clean_username(username)
    if cleaned is None:
        return {"available": True}
    own = db.query(UserProfile.username).filter(UserProfile.user_id == uid).scalar()
    if _same_username(own, cleaned):
        return {"available": True}
    return {"available": not _username_taken(cleaned, uid, db)}


@router.put("/profile", response_model=MyProfileResponse)
def update_my_profile(
    data: MyProfileUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    profile = _get_or_create_profile(uid, db)
    fields = data.model_dump(exclude_unset=True)
    if "username" in fields:
        username = _clean_username(data.username)
        # Solo se valida si el nombre cambia: quien ya tiene un nombre repetido (de antes de esta regla) puede seguir
        # guardando su perfil sin tocarlo
        if username is not None and not _same_username(profile.username, username) and _username_taken(username, uid, db):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese nombre de usuario ya está en uso.")
        profile.username = username
    if data.first_name is not None:
        profile.first_name = data.first_name
    if data.last_name is not None:
        profile.last_name = data.last_name
    rules_changed = False
    if data.stock_minimo is not None:
        rules_changed |= profile.stock_minimo != data.stock_minimo
        profile.stock_minimo = data.stock_minimo
    if data.dias_alerta_vencimiento is not None:
        rules_changed |= profile.dias_alerta_vencimiento != data.dias_alerta_vencimiento
        profile.dias_alerta_vencimiento = data.dias_alerta_vencimiento
    if data.alert_check_hour is not None:
        profile.alert_check_hour = ar_hour_to_utc(data.alert_check_hour)
    if data.user_type is not None:
        profile.user_type = data.user_type
    db.commit()
    db.refresh(profile)
    if rules_changed:
        # Con otra regla, las alertas de stock pueden ser otras: el app_backend las vuelve a evaluar, sin mandar avisos
        background_tasks.add_task(refresh_user_alerts, uid)
    return _profile_response(current.email, profile)


def _suggestion(p: Product) -> ProductSuggestion:
    return ProductSuggestion(
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
    return [_suggestion(p) for p in rows]


@router.get("/products/match", response_model=list[ProductSuggestion])
def match_products(
    q: str = Query(..., min_length=1, max_length=1000),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Los productos del usuario que se parecen a un texto leído de una etiqueta, de mejor a peor.

    A diferencia de `/products/suggest` (que busca dentro del nombre), compara el texto con el nombre, la marca y la
    descripción de cada producto, y tolera abreviaturas y palabras de más.
    """
    rows = db.query(Product).filter(Product.user_id == str(current.id)).all()
    return [_suggestion(p) for p, _ in matching.buscar_texto(q, rows)]


@router.get("/products/groups", response_model=list[AdminProductGroupSummary])
def my_product_groups(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """El stock total de «el mismo producto» (igual nombre y categoría, de cualquier marca) entre los del usuario, para
    los que el nombre contiene `q`. Como en la app, sin distinguir mayúsculas, tildes ni espacios sobrantes."""
    needle = alphabetical_key(" ".join(q.split()))
    if not needle:
        return []
    found = [
        admin_products._summarize(lots)
        for key, lots in admin_products._groups(db, str(current.id)).items()
        if needle in key[0]
    ]
    found.sort(key=lambda g: (alphabetical_key(g.nombre), alphabetical_key(g.categoria)))
    return found[:10]


@router.get("/products/group", response_model=AdminProductGroupDetail)
def my_product_group(
    nombre: str = Query(..., min_length=1),
    categoria: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Todos los productos del usuario con ese nombre y categoría y su stock total, por vencimiento."""
    uid = str(current.id)
    lots = admin_products._groups(db, uid).get(admin_products._group_key(nombre, categoria))
    if not lots:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    emails = {uid: current.email}
    ordered = sorted(lots, key=lambda p: (p.vencimiento is None, p.vencimiento or date.max, p.id))
    levels = alerts_view.levels_by_product(db, [p.id for p in ordered])
    return AdminProductGroupDetail(
        **admin_products._summarize(lots).model_dump(),
        productos=[admin_products._to_response(p, emails, levels) for p in ordered],
    )


def _naive_utc(value: datetime) -> datetime:
    """Los instantes se comparan sin zona (en UTC): la base devuelve unos con zona y otros sin ella."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# Las notas automáticas de las correcciones dicen cuándo se hizo el registro que corrigen (hora argentina), no su número
AR_TZ = timezone(timedelta(hours=-3))
_LEGACY_NOTE = re.compile(r"^(Corrección: revierte|Deshecho: revierte|Corrige) el registro #(\d+)(?: \(antes:.*\))?$")


def _event_stamp(moment: datetime) -> str:
    """«28/10/2026 15:29», en hora argentina."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")


def _clean_note(note, by_id: dict):
    """Las notas viejas mencionaban «el registro #43» y «(antes: ...)»: se muestran con la fecha del registro."""
    match = _LEGACY_NOTE.match(note or "")
    if not match:
        return note
    verb = "Deshace" if match.group(1).startswith("Deshecho") else "Corrige"
    target = by_id.get(int(match.group(2)))
    return f"{verb} el registro del {_event_stamp(target.consumed_at)}" if target else f"{verb} un registro anterior"


def _check_same_direction(event, new_event_type: str) -> None:
    """Una entrada sigue siendo entrada y una salida sigue siendo salida: solo cambian las unidades (y el motivo de la salida)."""
    if (event.event_type == "restocked") != (new_event_type == "restocked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Una entrada no puede pasar a ser una salida, ni una salida una entrada",
        )


def _correction_companion(reversal, by_id: dict):
    """Al corregir un registro se guardan, juntos, la reversión y el movimiento nuevo (el que sigue y es del mismo instante).
    Al deshacer no hay movimiento nuevo."""
    following = by_id.get(reversal.id + 1)
    if (
        following is not None
        and following.reverses_event_id is None
        and following.product_id == reversal.product_id
        and _naive_utc(following.consumed_at) == _naive_utc(reversal.consumed_at)
    ):
        return following
    return None


def _is_correction_companion(event, by_id: dict) -> bool:
    previous = by_id.get(event.id - 1)
    return previous is not None and previous.reverses_event_id is not None and _correction_companion(previous, by_id) is event


@router.get("/changes", response_model=list[StockChangeOut])
def my_stock_changes(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Los últimos cambios de stock del usuario, del más nuevo al más viejo (como «Últimos cambios» de la app).

    Incluye las entradas, las salidas y las modificaciones hechas al editar un producto. Cada cambio trae el stock antes
    y después, que se reconstruye hacia atrás desde el stock actual con los movimientos del producto. Las correcciones no
    aparecen: queda el movimiento corregido. `offset` salta los más nuevos para pedir las páginas siguientes.
    """
    uid = str(current.id)
    products = {p.id: p for p in db.query(Product).filter(Product.user_id == uid).all()}
    events = (
        db.query(ConsumptionEvent)
        .filter(ConsumptionEvent.user_id == uid)
        .order_by(ConsumptionEvent.consumed_at.desc(), ConsumptionEvent.id.desc())
        .all()
    )
    by_id = {e.id: e for e in events}
    after_of: dict[int, int] = {}

    running = {product_id: product.stock for product_id, product in products.items()}
    changes: list[tuple[tuple[datetime, int], StockChangeOut]] = []
    for event in events:
        product = products.get(event.product_id)
        if product is None:
            continue
        after = running[product.id]
        before = after - _stock_effect(event.event_type, event.quantity)
        running[product.id] = before
        after_of[event.id] = after
        producto = MyProductResponse.model_validate(product, from_attributes=True)
        if event.reverses_event_id is not None:
            # Una corrección o un deshacer deja UNA entrada de «modificación» con el efecto neto (35 → 33), y el movimiento
            # corregido sigue apareciendo con su stock de entonces (20 → 35)
            companion = _correction_companion(event, by_id)
            final_after = after_of[companion.id] if companion is not None else after
            changes.append((
                (_naive_utc(event.consumed_at), 1 + (companion.id if companion is not None else event.id)),
                StockChangeOut(
                    id=f"event-{event.id}", kind="modificacion", stock_before=before, stock_after=final_after,
                    quantity=abs(final_after - before), occurred_at=event.consumed_at, producto=producto,
                ),
            ))
            continue
        if _is_correction_companion(event, by_id):
            continue
        if (event.note or "").startswith(STOCK_EDIT_NOTE):
            kind = "modificacion"
        else:
            kind = "entrada" if event.event_type == "restocked" else "salida"
        changes.append((
            (_naive_utc(event.consumed_at), 1 + event.id),
            StockChangeOut(
                id=f"event-{event.id}", kind=kind, stock_before=before, stock_after=after, quantity=event.quantity,
                occurred_at=event.consumed_at, producto=producto,
            ),
        ))

    # Lo que queda después de deshacer todos los movimientos es el stock con el que se cargó el producto
    for product_id, product in products.items():
        initial = running[product_id]
        if product.created_at is None or initial <= 0:
            continue
        changes.append((
            (_naive_utc(product.created_at), 0),
            StockChangeOut(
                id=f"created-{product_id}", kind="entrada", stock_before=0, stock_after=initial, quantity=initial,
                occurred_at=product.created_at, producto=MyProductResponse.model_validate(product, from_attributes=True),
            ),
        ))

    changes.sort(key=lambda item: item[0], reverse=True)
    return [change for _, change in changes[offset:offset + limit]]


@router.get("/products/by-barcode/{codigo}", response_model=Optional[ProductSuggestion])
def product_by_barcode(
    codigo: str,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    """Qué producto tuyo tiene este código de barras, si alguno.

    Devuelve `null` en vez de 404 porque no encontrarlo es un resultado normal y
    esperado: es lo que le dice al formulario que abra un alta nueva con el
    código ya cargado, en lugar de un error que haya que interpretar.

    El código se normaliza antes de buscar por la misma razón por la que se
    normaliza al guardarlo: si no, el mismo envase leído por el escáner y escrito
    a mano no se encuentra a sí mismo.
    """
    normalizado = _normalizar_codigo(codigo)
    if not normalizado:
        return None

    product = (
        db.query(Product)
        .filter(Product.user_id == str(current.id), Product.codigo_barras == normalizado)
        .order_by(Product.vencimiento.asc().nullslast(), Product.id.asc())
        .first()
    )
    if not product:
        return None

    return ProductSuggestion(
        id=product.id,
        nombre=product.nombre,
        marca=product.marca,
        categoria=product.categoria,
        ubicacion=product.ubicacion,
        vencimiento=product.vencimiento,
        stock=product.stock,
        stock_minimo=product.stock_minimo,
        dias_alerta_vencimiento=product.dias_alerta_vencimiento,
    )


@router.get("/products/defaults", response_model=MyDefaults)
def my_defaults(
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    profile = db.query(UserProfile).filter(UserProfile.user_id == str(current.id)).first()
    if not profile:
        return MyDefaults()
    return MyDefaults(last_categoria=profile.last_categoria, last_ubicacion=profile.last_ubicacion)


def _normalizar_codigo(codigo):
    """Valida el código de barras y lo deja en su forma canónica.

    Un GTIN trae su propio dígito verificador, así que un código mal tipeado se
    detecta acá y no seis meses después, cuando el escáner no lo encuentre.
    """
    try:
        return barcode.normalizar(codigo)
    except barcode.CodigoInvalido as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/products", response_model=MyProductResponse, status_code=201)
def create_my_product(
    data: MyProductCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    uid = str(current.id)
    data.codigo_barras = _normalizar_codigo(data.codigo_barras)
    # Con un producto nuevo (o más stock de uno que ya estaba) las alertas de stock pueden cambiar
    background_tasks.add_task(refresh_user_alerts, uid)

    profile = _get_or_create_profile(uid, db)
    profile.last_categoria = data.categoria
    profile.last_ubicacion = data.ubicacion
    db.commit()

    existing = _find_matching_product(uid, data.nombre, data.marca, data.vencimiento, db)
    if existing:
        existing.stock += data.stock
        # Si el producto ya estaba pero sin código, se lo completa: es dato nuevo
        # sobre algo que ya existía, no un producto distinto.
        if data.codigo_barras and not existing.codigo_barras:
            existing.codigo_barras = data.codigo_barras
        db.commit()
        db.refresh(existing)
        _log_consumption(db, existing.id, uid, data.stock, "restocked", actor_email=current.email)
        _log_activity(
            db, "product_updated", current.email, uid, current.email,
            f"{current.email} sumó {data.stock} unidades a \"{existing.nombre}\" (stock: {existing.stock}) desde el dashboard",
        )
        return MyProductResponse.model_validate(existing, from_attributes=True).model_copy(update={"merged": True})

    product = Product(user_id=uid, **data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    _log_activity(
        db, "product_created", current.email, uid, current.email,
        f"{current.email} cargó \"{product.nombre}\" (stock: {product.stock}) desde el dashboard",
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
        f"{current.email} editó {len(products)} productos en lote ({campos}) desde el dashboard",
    )
    return MyBulkEditResult(updated=len(products))


def _delete_own_products(products: list, db: Session, current: AuthUser) -> None:
    """Borra los productos (y sus alertas; los movimientos se van por el ON DELETE CASCADE) y deja el registro de actividad."""
    uid = str(current.id)
    ids = [p.id for p in products]
    nombres = ", ".join(f'"{p.nombre}"' for p in products[:3]) + (f" y {len(products) - 3} más" if len(products) > 3 else "")
    # Sin ON DELETE CASCADE en alerts.product_id, Postgres rechaza el borrado si el producto tiene alertas.
    db.query(Alert).filter(Alert.product_id.in_(ids)).delete(synchronize_session=False)
    for product in products:
        db.delete(product)
    db.commit()
    _log_activity(
        db, "product_deleted", current.email, uid, current.email,
        f"{current.email} eliminó {nombres} desde el dashboard",
    )


@router.post("/products/bulk-delete", response_model=MyBulkDeleteResult)
def bulk_delete_my_products(
    data: MyBulkDeleteRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    if not data.product_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se seleccionó ningún producto")
    uid = str(current.id)
    products = db.query(Product).filter(Product.id.in_(data.product_ids), Product.user_id == uid).all()
    if not products:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontraron productos")
    _delete_own_products(products, db, current)
    background_tasks.add_task(refresh_user_alerts, uid)
    return MyBulkDeleteResult(deleted=len(products))


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_product(
    product_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    product = _get_own_product(product_id, str(current.id), db)
    _delete_own_products([product], db, current)
    background_tasks.add_task(refresh_user_alerts, str(current.id))


@router.patch("/products/{product_id}", response_model=MyProductResponse)
def update_my_product(
    product_id: int,
    data: MyProductUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    product = _get_own_product(product_id, str(current.id), db)
    update_dict = data.model_dump(exclude_unset=True)
    if "codigo_barras" in update_dict:
        update_dict["codigo_barras"] = _normalizar_codigo(update_dict["codigo_barras"])
    new_stock = update_dict.get("stock")
    old_stock = product.stock

    changed_fields = list(update_dict.keys())
    for field, value in update_dict.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)

    if new_stock is not None and new_stock != old_stock:
        if new_stock < old_stock:
            _log_consumption(db, product.id, str(current.id), old_stock - new_stock, "consumed", actor_email=current.email, note=STOCK_EDIT_NOTE)
        else:
            _log_consumption(db, product.id, str(current.id), new_stock - old_stock, "restocked", actor_email=current.email, note=STOCK_EDIT_NOTE)
    if any(field in update_dict for field in ("stock", "stock_minimo", "dias_alerta_vencimiento", "vencimiento")):
        background_tasks.add_task(refresh_user_alerts, str(current.id))

    if changed_fields:
        _log_activity(
            db, "product_updated", current.email, str(current.id), current.email,
            f"{current.email} editó \"{product.nombre}\" ({', '.join(changed_fields)}) desde el dashboard",
        )
    return product


@router.post("/products/salida", response_model=MyProductResponse)
def register_salida(
    data: SalidaRequest,
    background_tasks: BackgroundTasks,
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
    background_tasks.add_task(refresh_user_alerts, uid)
    motivo = {"expired": "vencido", "sold": "vendido", "rented": "alquilado"}.get(data.event_type, "consumido")
    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} sacó {data.quantity} unidades de \"{product.nombre}\" ({motivo}, stock: {product.stock}) desde el dashboard",
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
    by_id = {e.id: e for e in events}
    return [
        ConsumptionEventOut(
            id=e.id,
            quantity=e.quantity,
            event_type=e.event_type,
            consumed_at=e.consumed_at,
            note=_clean_note(e.note, by_id),
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
    _check_same_direction(event, data.event_type)

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
        note=f"Corrige el registro del {_event_stamp(event.consumed_at)}",
        reverses_event_id=event.id,
    ))
    new_event = ConsumptionEvent(
        product_id=product.id,
        user_id=product.user_id,
        quantity=data.quantity,
        event_type=data.event_type,
        actor_email=current.email,
        note=data.note or f"Corrige el registro del {_event_stamp(event.consumed_at)}",
    )
    db.add(new_event)
    product.stock = new_stock
    db.commit()
    db.refresh(product)
    db.refresh(new_event)

    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} corrigió un consumo de \"{product.nombre}\" "
        f"(de {event.quantity} a {data.quantity} unidades) desde el dashboard",
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
        note=f"Deshace el registro del {_event_stamp(event.consumed_at)}",
        reverses_event_id=event.id,
    ))
    product.stock = new_stock
    db.commit()
    db.refresh(product)

    _log_activity(
        db, "product_updated", current.email, uid, current.email,
        f"{current.email} deshizo un registro de consumo de \"{product.nombre}\" "
        f"({quantity} unidades, {motivo}) desde el dashboard",
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
    elif len(nombre) > MAX_NOMBRE:
        errors.append(f"nombre supera los {MAX_NOMBRE} caracteres")

    categoria = _clean_str(raw.get("categoria"))
    if mode == "entrada" and not categoria:
        errors.append("categoria es obligatoria")
    elif categoria and len(categoria) > MAX_CATEGORIA:
        errors.append(f"categoria supera los {MAX_CATEGORIA} caracteres")

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

    codigo_barras, codigo_error = _coerce_codigo(raw.get("codigo_barras"))
    if codigo_error:
        errors.append(codigo_error)

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
        codigo_barras=codigo_barras,
        stock_minimo=stock_minimo,
        dias_alerta_vencimiento=dias_alerta,
    )
    return BulkParsedRow(row_number=row_number, data=data, valid=not errors, errors=errors)


def _coerce_codigo(raw) -> tuple[Optional[str], Optional[str]]:
    """
    El código de barras de una fila del archivo, validado igual que en el alta.

    Excel convierte en número una celda con dígitos: `7794640172021` llega como
    entero —y a veces como `7794640172021.0`—, así que se lo pasa a texto sin
    decimales antes de validar. Lo que no se puede recuperar es un cero adelante
    que Excel ya se comió: esa fila vuelve con el error del largo, que es mejor
    que guardar un código que no va a coincidir con el escáner.
    """
    if raw is None:
        return None, None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    try:
        return barcode.normalizar(str(raw)), None
    except barcode.CodigoInvalido as e:
        return None, f"codigo_barras: {e}"


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


def _validar_fila(row: BulkProductRow, mode: str) -> None:
    """Las reglas del alta, aplicadas a cada fila del commit.

    El commit no puede confiar en que el cliente pasó antes por `/bulk/parse`: es
    un endpoint más de la API, y una fila con stock negativo sumaría stock al
    descontar o dejaría un producto en negativo al cargar.
    """
    if row.stock <= 0:
        raise ValueError("stock debe ser mayor a 0")
    if not row.nombre.strip():
        raise ValueError("nombre es obligatorio")
    if len(row.nombre) > MAX_NOMBRE:
        raise ValueError(f"nombre supera los {MAX_NOMBRE} caracteres")
    if mode == "entrada":
        if not row.categoria.strip():
            raise ValueError("categoria es obligatoria")
        if len(row.categoria) > MAX_CATEGORIA:
            raise ValueError(f"categoria supera los {MAX_CATEGORIA} caracteres")
        if row.descripcion and len(row.descripcion) > MAX_DESCRIPCION:
            raise ValueError(f"descripcion supera los {MAX_DESCRIPCION} caracteres")


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
            _validar_fila(row, body.mode)
            if body.mode == "salida":
                existing = _find_matching_product(uid, row.nombre, row.marca, row.vencimiento, db)
                if not existing:
                    raise ValueError("no existe un producto tuyo con ese nombre/marca/vencimiento")
                if row.stock > existing.stock:
                    raise ValueError(f"no hay suficiente stock disponible (hay {existing.stock})")
                existing.stock -= row.stock
                db.commit()
                _log_consumption(db, existing.id, uid, row.stock, "consumed", actor_email=current.email, note=body.origen)
                merged += 1
                _log_activity(
                    db, "product_updated", current.email, uid, current.email,
                    f"{current.email} sacó {row.stock} unidades de \"{existing.nombre}\" (stock: {existing.stock}) por {body.origen.lower()}",
                )
                continue

            existing = _find_matching_product(uid, row.nombre, row.marca, row.vencimiento, db)
            if existing:
                existing.stock += row.stock
                if row.codigo_barras and not existing.codigo_barras:
                    existing.codigo_barras = _normalizar_codigo(row.codigo_barras)
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
                    codigo_barras=_normalizar_codigo(row.codigo_barras),
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
                _log_consumption(db, existing.id, uid, row.stock, "restocked", actor_email=current.email, note=body.origen)
            _log_activity(
                db, "product_created" if kind_log == "cargó" else "product_updated",
                current.email, uid, current.email,
                f"{current.email} {kind_log} \"{nombre_log}\" (stock: {stock_log}) por {body.origen.lower()}",
            )
        except HTTPException as e:
            # Un código de barras inválido llega como HTTPException: `str(e)`
            # antepone el status ("400: ...") a un mensaje que lee el usuario.
            db.rollback()
            failed += 1
            errors.append(f"Fila {row.row_number}: {e.detail}")
        except Exception as e:
            db.rollback()
            failed += 1
            errors.append(f"Fila {row.row_number}: {e}")

    return BulkCommitResult(created=created, merged=merged, failed=failed, errors=errors)
