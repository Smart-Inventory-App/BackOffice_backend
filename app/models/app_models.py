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
    # False en las cuentas creadas con Google que todavía no definieron una contraseña
    has_password = Column(Boolean, default=True, nullable=False, server_default="true")
    # True en una cuenta que un administrador creó con una contraseña inicial: en su primer ingreso tiene que cambiarla
    must_change_password = Column(Boolean, default=False, nullable=False, server_default="false")
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
    # Días de cobertura mínimos (stock mínimo) y días de anticipación del vencimiento, para todos sus productos
    stock_minimo = Column(Integer, nullable=True)
    dias_alerta_vencimiento = Column(Integer, nullable=True)
    # Hora del chequeo diario de alertas, en UTC
    alert_check_hour = Column(Integer, nullable=True)
    user_type = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# Salidas que cuentan como demanda real (métricas): uso, venta y alquiler. "expired" es una pérdida.
CONSUMPTION_EVENT_TYPES = ("consumed", "sold", "rented")


class ConsumptionEvent(Base):
    """Owned by `app_backend` — table `consumption_events`. Registra cada
    movimiento de stock (entrada/salida) para poder analizar consumo."""

    __tablename__ = "consumption_events"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    # "consumed": bajó el stock por uso; "sold": se vendió; "rented": se alquiló; "expired": venció;
    # "restocked": entró stock nuevo
    event_type = Column(String, nullable=False, server_default="consumed")
    consumed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Quién generó el movimiento (distinto de user_id, que es de quién es el inventario)
    actor_email = Column(String, nullable=True)
    # Motivo libre opcional
    note = Column(String, nullable=True)
    # Ledger append-only: si está seteado, esta fila revierte el efecto de otra fila
    reverses_event_id = Column(Integer, ForeignKey("consumption_events.id", ondelete="SET NULL"), nullable=True)


class Alert(Base):
    """Owned by `app_backend` — table `alerts`. Se lee para mostrar las alertas del modelo predictivo y se usa para borrar
    las de un producto antes de eliminarlo (la FK no tiene ON DELETE CASCADE)."""

    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("auth_users.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    type = Column(String(50), nullable=False)
    level = Column(String(50), nullable=False)
    units_wasted = Column(Integer, nullable=True)
    days_until_stockout = Column(Integer, nullable=True)
    suggested_qty = Column(Integer, nullable=True)


# Días de cobertura mínimos por defecto (el stock mínimo se mide en días, como en la app)
DEFAULT_STOCK_MINIMO = 7
DEFAULT_DIAS_ALERTA_VENCIMIENTO = 7
# Hora del chequeo diario por defecto, en UTC (las 5 de la mañana argentinas)
DEFAULT_ALERT_CHECK_HOUR = 8
DEFAULT_USER_TYPE = "individuo"
