from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, Product, UserProfile
from app.models.audit import UserStatusAudit
from app.schemas.platform import (
    AdminProductResponse,
    AppUserDetail,
    AppUserResponse,
    AuditEntry,
    PaginatedUsers,
    ProductOwner,
    UserStatusUpdate,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


def _product_counts(db: Session) -> dict[str, int]:
    rows = db.query(Product.user_id, func.count(Product.id)).group_by(Product.user_id).all()
    return {uid: count for uid, count in rows}


def _profiles(db: Session) -> dict[str, UserProfile]:
    return {p.user_id: p for p in db.query(UserProfile).all()}


def _admin_emails(db: Session) -> list[str]:
    """Emails que son administradores del backoffice (se excluyen del listado de
    usuarios de la app: son cuentas de auth pero no usuarios finales)."""
    return [row[0] for row in db.query(AdminUser.email).all()]


@router.get("/", response_model=PaginatedUsers)
def list_users(
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, pattern="^(active|inactive)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    query = db.query(AuthUser)
    admin_emails = _admin_emails(db)
    if admin_emails:
        query = query.filter(AuthUser.email.notin_(admin_emails))
    if search:
        query = query.filter(AuthUser.email.ilike(f"%{search}%"))
    if status_filter == "active":
        query = query.filter(AuthUser.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(AuthUser.is_active.is_(False))

    total = query.count()
    users = (
        query.order_by(AuthUser.created_at.desc().nullslast())
        .offset(offset)
        .limit(limit)
        .all()
    )
    counts = _product_counts(db)
    profiles = _profiles(db)

    result = []
    for u in users:
        uid = str(u.id)
        prof = profiles.get(uid)
        result.append(
            AppUserResponse(
                id=uid,
                email=u.email,
                is_active=bool(u.is_active),
                created_at=u.created_at,
                username=prof.username if prof else None,
                first_name=prof.first_name if prof else None,
                last_name=prof.last_name if prof else None,
                product_count=counts.get(uid, 0),
            )
        )
    return PaginatedUsers(items=result, total=total)


@router.get("/{user_id}", response_model=AppUserDetail)
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    user = db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    uid = str(user.id)
    prof = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    products = db.query(Product).filter(Product.user_id == uid).order_by(Product.created_at.desc()).all()

    return AppUserDetail(
        id=uid,
        email=user.email,
        is_active=bool(user.is_active),
        created_at=user.created_at,
        username=prof.username if prof else None,
        first_name=prof.first_name if prof else None,
        last_name=prof.last_name if prof else None,
        product_count=len(products),
        products=[
            AdminProductResponse(
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
                owner=ProductOwner(user_id=uid, email=user.email),
            )
            for p in products
        ],
    )


@router.patch("/{user_id}/status", response_model=AppUserResponse)
def update_user_status(
    user_id: str,
    data: UserStatusUpdate,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    user = db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    user.is_active = data.is_active

    reason = (data.reason or "").strip() or None
    db.add(
        UserStatusAudit(
            user_id=str(user.id),
            changed_by=current_admin.email,
            is_active=data.is_active,
            reason=reason,
        )
    )
    db.commit()
    db.refresh(user)

    uid = str(user.id)
    prof = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    counts = _product_counts(db)
    return AppUserResponse(
        id=uid,
        email=user.email,
        is_active=bool(user.is_active),
        created_at=user.created_at,
        username=prof.username if prof else None,
        first_name=prof.first_name if prof else None,
        last_name=prof.last_name if prof else None,
        product_count=counts.get(uid, 0),
    )


@router.get("/{user_id}/audit", response_model=list[AuditEntry])
def get_user_audit(
    user_id: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    return (
        db.query(UserStatusAudit)
        .filter(UserStatusAudit.user_id == user_id)
        .order_by(UserStatusAudit.created_at.desc())
        .all()
    )
