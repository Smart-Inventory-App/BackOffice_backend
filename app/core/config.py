from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    """Backoffice backend settings, loaded from environment variables."""

    API_TITLE: str = "Smart Inventory — Backoffice API"
    API_VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Shared database with the rest of the platform (auth_users, products, user_profiles)
    DATABASE_URL: str = "postgresql://user:password@localhost:5432/tpp_db"

    # Dedicated JWT for admin sessions (independent from the app users' JWT_SECRET)
    ADMIN_JWT_SECRET: str = "admin-secret-change-in-production"
    ADMIN_JWT_ALGORITHM: str = "HS256"
    ADMIN_JWT_EXPIRE_MINUTES: int = 120

    # CORS — comma-separated origins allowed to call this API (the backoffice frontend)
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
