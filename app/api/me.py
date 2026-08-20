import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.app_models import AuthUser, Product, UserProfile
from app.schemas.platform import (
    BulkCommitRequest,
    BulkCommitResult,
    BulkParsedRow,
    BulkParseResponse,
    BulkProductRow,
    CategoryCount,
    MyDefaults,
    MyProductCreate,
    MyProductResponse,
    MyStats,
    PaginatedMyProducts,
    ProductSuggestion,
)

router = APIRouter(prefix="/me", tags=["me"])

REQUIRED_FIELDS = ("nombre", "categoria", "stock")


def _get_or_create_profile(user_id: str, db: Session) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        profile = UserProfile(user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


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
    own = db.query(Product).filter(Product.user_id == uid)

    total_products = own.count()
    products_with_image = own.filter(Product.imagen_url.isnot(None), Product.imagen_url != "").count()
    low_stock = own.filter(Product.stock <= 1).count()
    expiring_soon = own.filter(
        Product.vencimiento >= today, Product.vencimiento <= today + timedelta(days=7)
    ).count()
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
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    query = db.query(Product).filter(Product.user_id == str(current.id))
    if search:
        term = f"%{search}%"
        query = query.filter(Product.nombre.ilike(term) | Product.marca.ilike(term))
    if categoria:
        query = query.filter(Product.categoria.ilike(categoria))

    total = query.count()
    products = query.order_by(Product.created_at.desc()).offset(offset).limit(limit).all()
    return PaginatedMyProducts(items=products, total=total)


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


@router.get("/products/suggest", response_model=list[ProductSuggestion])
def suggest_products(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current: AuthUser = Depends(get_current_user),
):
    rows = (
        db.query(Product)
        .filter(Product.user_id == str(current.id), Product.nombre.ilike(f"%{q}%"))
        .order_by(Product.created_at.desc())
        .limit(50)
        .all()
    )
    seen: set[str] = set()
    suggestions: list[ProductSuggestion] = []
    for p in rows:
        key = p.nombre.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(
            ProductSuggestion(nombre=p.nombre, marca=p.marca, categoria=p.categoria, ubicacion=p.ubicacion)
        )
        if len(suggestions) >= 8:
            break
    return suggestions


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
        return MyProductResponse.model_validate(existing, from_attributes=True).model_copy(update={"merged": True})

    product = Product(user_id=uid, **data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


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


def _validate_row(raw: dict, row_number: int) -> BulkParsedRow:
    errors: list[str] = []

    nombre = _clean_str(raw.get("nombre"))
    if not nombre:
        errors.append("nombre es obligatorio")

    categoria = _clean_str(raw.get("categoria"))
    if not categoria:
        errors.append("categoria es obligatoria")

    stock, stock_error = _coerce_stock(raw.get("stock"))
    if stock_error:
        errors.append(stock_error)

    vencimiento, venc_error = _coerce_vencimiento(raw.get("vencimiento"))
    if venc_error:
        errors.append(venc_error)

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
        result = _validate_row(raw, idx)
        if result.valid and result.data:
            marca_key = result.data.marca.strip().lower() if result.data.marca else None
            key = (result.data.nombre.strip().lower(), marca_key, result.data.vencimiento)
            if key in local_stock:
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
            existing = _find_matching_product(uid, row.nombre, row.marca, row.vencimiento, db)
            if existing:
                existing.stock += row.stock
                merged += 1
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
                )
                db.add(product)
                created += 1
            db.commit()
        except Exception as e:
            db.rollback()
            failed += 1
            errors.append(f"Fila {row.row_number}: {e}")

    return BulkCommitResult(created=created, merged=merged, failed=failed, errors=errors)
