from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    create_admin_token,
    get_current_admin,
    hash_password,
    verify_password,
)
from app.models.admin import AdminUser
from app.schemas.admin import AdminCreate, AdminLogin, AdminResponse, TokenResponse

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


@router.post("/login", response_model=TokenResponse)
def login(data: AdminLogin, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter(AdminUser.email == data.email).first()
    if not admin or not verify_password(data.password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )
    if not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrador deshabilitado",
        )
    token = create_admin_token(admin.id, admin.email)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=AdminResponse)
def me(current: AdminUser = Depends(get_current_admin)):
    return current


@router.get("/admins", response_model=list[AdminResponse])
def list_admins(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    return db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()


@router.post("/admins", response_model=AdminResponse, status_code=status.HTTP_201_CREATED)
def create_admin(
    data: AdminCreate,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    if db.query(AdminUser).filter(AdminUser.email == data.email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un administrador con ese email",
        )
    admin = AdminUser(
        email=data.email,
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin
