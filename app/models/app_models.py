"""Read-only mirrors of tables owned by other services in the shared tpp_db.

These models intentionally match the schema defined by the `auth` and
`app_backend` services. They are NOT created by this service (the owning
services already create them); the backoffice only reads/manages rows.
"""
import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AuthUser(Base):
    """Owned by the `auth` service — table `auth_users`."""

    __tablename__ = "auth_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime)


class Product(Base):
    """Owned by `app_backend` — table `products`. `user_id` holds the auth user UUID as text."""

    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    nombre = Column(String, nullable=False)
    marca = Column(String, nullable=True)
    categoria = Column(String, nullable=False)
    stock = Column(Integer, nullable=False)
    vencimiento = Column(Date, nullable=True)
    descripcion = Column(String, nullable=True)
    imagen_url = Column(String, nullable=True)
    ubicacion = Column(String, nullable=True)
    codigo_barras = Column(String(14), index=True, nullable=True)
    stock_minimo = Column(Integer, nullable=True)
    dias_alerta_vencimiento = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class UserProfile(Base):
    """Owned by `app_backend` — table `user_profiles`."""

    __tablename__ = "user_profiles"

    user_id = Column(String, primary_key=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    last_categoria = Column(String, nullable=True)
    last_ubicacion = Column(String, nullable=True)
    stock_minimo = Column(Integer, nullable=True)
    dias_alerta_vencimiento = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ConsumptionEvent(Base):
    """Owned by `app_backend` — table `consumption_events`. Registra cada
    movimiento de stock (entrada/salida) para poder analizar consumo."""

    __tablename__ = "consumption_events"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    # "consumed": bajó el stock por uso/vencimiento; "restocked": entró stock nuevo
    event_type = Column(String, nullable=False, server_default="consumed")
    consumed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Quién generó el movimiento (distinto de user_id, que es de quién es el inventario)
    actor_email = Column(String, nullable=True)
    # Motivo libre opcional
    note = Column(String, nullable=True)
    # Ledger append-only: si está seteado, esta fila revierte el efecto de otra fila
    reverses_event_id = Column(Integer, ForeignKey("consumption_events.id", ondelete="SET NULL"), nullable=True)


DEFAULT_STOCK_MINIMO = 10
DEFAULT_DIAS_ALERTA_VENCIMIENTO = 7
