from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, Product
from app.schemas.platform import (
    CategoryCount,
    DashboardStats,
    DayCount,
    UserProductCount,
)

router = APIRouter(prefix="/admin/stats", tags=["admin-stats"])


@router.get("/", response_model=DashboardStats)
def dashboard(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    today = date.today()
    now = datetime.utcnow()

    total_users = db.query(func.count(AuthUser.id)).scalar() or 0
    active_users = (
        db.query(func.count(AuthUser.id))
        .filter(AuthUser.is_active.is_(True))
        .scalar()
        or 0
    )
    new_users_last_7d = (
        db.query(func.count(AuthUser.id))
        .filter(AuthUser.created_at >= now - timedelta(days=7))
        .scalar()
        or 0
    )

    total_products = db.query(func.count(Product.id)).scalar() or 0
    products_with_image = (
        db.query(func.count(Product.id))
        .filter(Product.imagen_url.isnot(None), Product.imagen_url != "")
        .scalar()
        or 0
    )
    low_stock = db.query(func.count(Product.id)).filter(Product.stock <= 1).scalar() or 0
    expiring_soon = (
        db.query(func.count(Product.id))
        .filter(Product.vencimiento >= today, Product.vencimiento <= today + timedelta(days=7))
        .scalar()
        or 0
    )
    expired = (
        db.query(func.count(Product.id)).filter(Product.vencimiento < today).scalar() or 0
    )

    by_category = [
        CategoryCount(label=row[0] or "Sin categoría", count=row[1])
        for row in db.query(Product.categoria, func.count(Product.id))
        .group_by(Product.categoria)
        .order_by(func.count(Product.id).desc())
        .all()
    ]

    email_map = {str(u.id): u.email for u in db.query(AuthUser.id, AuthUser.email).all()}
    top_rows = (
        db.query(Product.user_id, func.count(Product.id))
        .group_by(Product.user_id)
        .order_by(func.count(Product.id).desc())
        .limit(5)
        .all()
    )
    top_users = [
        UserProductCount(user_id=uid, email=email_map.get(uid), count=count)
        for uid, count in top_rows
    ]

    # Products created per day over the last 14 days (zero-filled)
    raw = (
        db.query(func.date(Product.created_at), func.count(Product.id))
        .filter(Product.created_at >= now - timedelta(days=14))
        .group_by(func.date(Product.created_at))
        .all()
    )
    per_day_map = {str(d): c for d, c in raw}
    per_day = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        per_day.append(DayCount(day=d.isoformat(), count=per_day_map.get(d.isoformat(), 0)))

    return DashboardStats(
        total_users=total_users,
        active_users=active_users,
        inactive_users=total_users - active_users,
        new_users_last_7d=new_users_last_7d,
        total_products=total_products,
        products_with_image=products_with_image,
        low_stock_products=low_stock,
        expiring_soon_products=expiring_soon,
        expired_products=expired,
        products_by_category=by_category,
        top_users_by_products=top_users,
        products_per_day_last_14d=per_day,
    )
