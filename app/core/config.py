from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    """Backoffice backend settings, loaded from environment variables."""

    API_TITLE: str = "Smart Inventory — Backoffice API"
    API_VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Shared database with the rest of the platform (auth_users, products, user_profiles)
    DATABASE_URL: str = "postgresql://user:password@localhost:5432/tpp_db"

    # Auth service — los admins se autentican aquí, igual que los usuarios de la app.
    AUTH_SERVICE_URL: str = "http://localhost:8001"

    # JWT compartido con `auth` y `app_backend`. El backoffice SOLO valida tokens
    # emitidos por el servicio auth (no los crea), igual que hace app_backend.
    JWT_SECRET: str = "dev-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"

    # CORS — orígenes del frontend del backoffice
    ALLOWED_ORIGINS_STR: str = "http://localhost:3000,http://localhost:5173"

    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS_STR.split(",") if o.strip()]

    HOST: str = "0.0.0.0"
    PORT: int = 8002

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
