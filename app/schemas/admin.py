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
    # Si para darlo de alta se creó su cuenta (con la contraseña inicial) o ya la tenía
    account_created: bool = False

    class Config:
        from_attributes = True


class AdminCreate(BaseModel):
    """Alta de admin: email, nombre y, si el email no tiene cuenta, la contraseña inicial que el administrador que da de
    alta le hace llegar. Con ella entra por primera vez y el sistema le pide cambiarla."""
    email: EmailStr
    full_name: Optional[str] = None
    initial_password: Optional[str] = None


class CheckEmailRequest(BaseModel):
    email: EmailStr


class CheckEmailResponse(BaseModel):
    exists: bool
    is_admin: bool
    password_set: bool
    # Cuenta creada con Google que todavía no tiene contraseña: tiene que crear una desde «Olvidé mi contraseña»
    google_only: bool = False


class MeResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: Optional[str] = None
    is_admin: bool
    is_active: bool
    # La cuenta tiene una contraseña inicial que le dio un administrador: antes de seguir tiene que cambiarla
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


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


class GoogleLoginRequest(BaseModel):
    """El idToken que Google le dio a quien tocó «Continuar con Google»: `auth` lo verifica."""
    id_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str


class ResetTokenResponse(BaseModel):
    reset_token: str
