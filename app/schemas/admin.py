from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminResponse(BaseModel):
    id: int
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool
    password_set: bool
    created_at: Optional[datetime] = None
    # Solo al dar de alta un admin sin cuenta: el código con el que define su contraseña.
    invite_token: Optional[str] = None

    class Config:
        from_attributes = True


class AdminCreate(BaseModel):
    """Alta de admin: solo email y nombre. La contraseña la define el propio
    admin en su primer inicio de sesión (vía el servicio auth)."""
    email: EmailStr
    full_name: Optional[str] = None


class CheckEmailRequest(BaseModel):
    email: EmailStr


class CheckEmailResponse(BaseModel):
    exists: bool
    is_admin: bool
    password_set: bool


class MeResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: Optional[str] = None
    is_admin: bool
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SetPasswordRequest(BaseModel):
    email: EmailStr
    password: str
    invite_token: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
