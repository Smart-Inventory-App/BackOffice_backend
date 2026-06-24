from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.core.database import Base


class AdminUser(Base):
    """Backoffice administrators. Owned and managed by this service only."""

    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
