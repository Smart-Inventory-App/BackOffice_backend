import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.auth import _auth_request
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.core import alerts_view
from app.core.argentina import AR_TZ, ar_today
from app.models.app_models import CONSUMPTION_EVENT_TYPES, AuthUser, ConsumptionEvent, Product, UserProfile
from app.models.audit import AdminRoleAudit, UserStatusAudit
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


def _user_uuid(user_id: str) -> uuid.UUID:
    """El id de la ruta como UUID. Uno mal formado es un usuario que no existe, no un error del servidor."""
    try:
        return uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")


def _product_counts(db: Session) -> dict[str, int]:
    rows = db.query(Product.user_id, func.count(Product.id)).group_by(Product.user_id).all()
    return {uid: count for uid, count in rows}


def _profiles(db: Session) -> dict[str, UserProfile]:
    return {p.user_id: p for p in db.query(UserProfile).all()}


def _admin_emails(db: Session) -> set[str]:
    """Emails que son administradores del backoffice, para marcarlos en el listado
    de usuarios de la app (ya no se excluyen: los admins también son cuentas
    reales y deben verse)."""
    return {row[0] for row in db.query(AdminUser.email).all()}


@router.get("/", response_model=PaginatedUsers)
def list_users(
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, pattern="^(active|inactive|admin)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    admin_emails = _admin_emails(db)

    query = db.query(AuthUser)
    if search:
        query = query.filter(AuthUser.email.ilike(f"%{search}%"))
    if status_filter == "active":
        query = query.filter(AuthUser.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(AuthUser.is_active.is_(False))
    elif status_filter == "admin":
        query = query.filter(AuthUser.email.in_(admin_emails))

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
                is_admin=u.email in admin_emails,
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
    user = db.query(AuthUser).filter(AuthUser.id == _user_uuid(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    uid = str(user.id)
    prof = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    products = db.query(Product).filter(Product.user_id == uid).order_by(Product.created_at.desc()).all()
    alerts = alerts_view.alerts_for(db, uid)
    summary = alerts_view.summarize(alerts)
    levels = alerts_view.levels_by_product(db, [p.id for p in products])

    # Consumo y reposición de los últimos 30 días contando hoy (días argentinos), sin las correcciones del historial
    month_start = datetime.combine(ar_today() - timedelta(days=29), time.min, tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    reversed_ids = db.query(ConsumptionEvent.reverses_event_id).filter(ConsumptionEvent.reverses_event_id.isnot(None))
    movements = (
        db.query(ConsumptionEvent.event_type, func.coalesce(func.sum(ConsumptionEvent.quantity), 0))
        .filter(
            ConsumptionEvent.user_id == uid,
            ConsumptionEvent.consumed_at >= month_start,
            ConsumptionEvent.reverses_event_id.is_(None),
            ConsumptionEvent.id.notin_(reversed_ids),
        )
        .group_by(ConsumptionEvent.event_type)
        .all()
    )

    return AppUserDetail(
        id=uid,
        email=user.email,
        is_active=bool(user.is_active),
        is_admin=user.email in _admin_emails(db),
        created_at=user.created_at,
        username=prof.username if prof else None,
        first_name=prof.first_name if prof else None,
        last_name=prof.last_name if prof else None,
        product_count=len(products),
        total_units=sum(p.stock for p in products),
        critical_alerts=summary.critical,
        warning_alerts=summary.warning,
        consumed_last_30d=sum(int(qty) for kind, qty in movements if kind in CONSUMPTION_EVENT_TYPES),
        restocked_last_30d=sum(int(qty) for kind, qty in movements if kind == "restocked"),
        alerts=alerts,
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
                codigo_barras=p.codigo_barras,
                stock_minimo=p.stock_minimo,
                dias_alerta_vencimiento=p.dias_alerta_vencimiento,
                created_at=p.created_at,
                alert_level=levels.get(p.id, (None, []))[0],
                alert_types=levels.get(p.id, (None, []))[1],
                owner=ProductOwner(user_id=uid, email=user.email, is_active=bool(user.is_active)),
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
    user = db.query(AuthUser).filter(AuthUser.id == _user_uuid(user_id)).first()
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
        is_admin=user.email in _admin_emails(db),
        created_at=user.created_at,
        username=prof.username if prof else None,
        first_name=prof.first_name if prof else None,
        last_name=prof.last_name if prof else None,
        product_count=counts.get(uid, 0),
    )


@router.post("/{user_id}/admin", response_model=AppUserResponse, status_code=status.HTTP_201_CREATED)
def grant_admin(
    user_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Hace admin a un usuario ya existente, elegido desde el listado. La
    escritura la hace `auth` (dueño de `admin_users`); reenviamos el bearer
    para que valide ahí también que el caller es admin."""
    user = db.query(AuthUser).filter(AuthUser.id == _user_uuid(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    if db.query(AdminUser).filter(AdminUser.email == user.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese usuario ya es administrador")

    prof = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    full_name = " ".join(filter(None, [prof.first_name if prof else None, prof.last_name if prof else None])) or None

    r = _auth_request(
        "POST",
        "/auth/admin/admins",
        json={"email": user.email, "full_name": full_name},
        headers={"Authorization": authorization},
    )
    if r.status_code == 409:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese usuario ya es administrador")
    if r.status_code != 201:
        raise HTTPException(status_code=r.status_code, detail="No se pudo otorgar el rol de administrador")

    db.add(
        AdminRoleAudit(
            user_id=str(user.id),
            changed_by=current_admin.email,
            action="granted",
        )
    )
    db.commit()

    uid = str(user.id)
    counts = _product_counts(db)
    return AppUserResponse(
        id=uid,
        email=user.email,
        is_active=bool(user.is_active),
        is_admin=True,
        created_at=user.created_at,
        username=prof.username if prof else None,
        first_name=prof.first_name if prof else None,
        last_name=prof.last_name if prof else None,
        product_count=counts.get(uid, 0),
    )


@router.delete("/{user_id}/admin", response_model=AppUserResponse)
def remove_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
):
    user = db.query(AuthUser).filter(AuthUser.id == _user_uuid(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    admin = db.query(AdminUser).filter(AdminUser.email == user.email).first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ese usuario no es administrador")
    if admin.email == current_admin.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No podés quitarte tu propio rol de administrador",
        )
    active_admins = db.query(func.count(AdminUser.id)).filter(AdminUser.is_active.is_(True)).scalar() or 0
    if admin.is_active and active_admins <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede quitar al último administrador",
        )

    db.delete(admin)
    db.add(
        AdminRoleAudit(
            user_id=str(user.id),
            changed_by=current_admin.email,
            action="revoked",
        )
    )
    db.commit()

    uid = str(user.id)
    prof = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
    counts = _product_counts(db)
    return AppUserResponse(
        id=uid,
        email=user.email,
        is_active=bool(user.is_active),
        is_admin=False,
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
    status_entries = [
        AuditEntry(
            id=e.id,
            user_id=e.user_id,
            changed_by=e.changed_by,
            kind="status",
            is_active=e.is_active,
            reason=e.reason,
            created_at=e.created_at,
        )
        for e in db.query(UserStatusAudit).filter(UserStatusAudit.user_id == user_id).all()
    ]
    role_entries = [
        AuditEntry(
            id=e.id,
            user_id=e.user_id,
            changed_by=e.changed_by,
            kind="role",
            action=e.action,
            created_at=e.created_at,
        )
        for e in db.query(AdminRoleAudit).filter(AdminRoleAudit.user_id == user_id).all()
    ]
    combined = status_entries + role_entries
    combined.sort(key=lambda e: e.created_at or datetime.min, reverse=True)
    return combined
