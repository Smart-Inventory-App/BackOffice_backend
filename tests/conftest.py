import os

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")

import os
import uuid

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://test:test@localhost:5432/tpp_test"
)

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api import auth as auth_api
from app.core.database import Base, get_db
from app.core.security import get_current_user
from app.main import app
from app.models.admin import AdminUser
from app.models.app_models import AuthUser, Product


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(os.environ["DATABASE_URL"], future=True)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    connection = engine.connect()
    outer_transaction = connection.begin()
    Session = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = Session()
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    yield session

    session.close()
    outer_transaction.rollback()
    connection.close()


@pytest.fixture()
def auth_user(db_session):
    user = AuthUser(
        id=uuid.uuid4(),
        email="test@example.com",
        hashed_password="x",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def client(db_session, auth_user):
    def override_get_db():
        yield db_session

    def override_get_current_user():
        return auth_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def create_product(db_session, auth_user, **overrides):
    defaults = dict(
        user_id=str(auth_user.id),
        nombre="Leche entera",
        marca="La Serenísima",
        categoria="Lácteos",
        stock=10,
        vencimiento=None,
        ubicacion=None,
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


@pytest.fixture()
def auth_service(monkeypatch):
    """Instala un servicio `auth` de mentira y devuelve las peticiones que recibió.

    Usado por cualquier endpoint que delegue en `auth` por HTTP (altas de
    admin, set-password, login): `app/api/auth.py` y `app/api/users.py`
    comparten el mismo `httpx.Client` sincrónico vía `_auth_request`.
    """
    def install(handler):
        received = []

        def record(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return handler(request)

        class FakeClient(httpx.Client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(record)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(auth_api.httpx, "Client", FakeClient)
        return received

    return install


def create_admin(db_session, **overrides):
    defaults = dict(
        email="admin@smartinventory.com",
        full_name="Admin de prueba",
        is_active=True,
        password_set=True,
    )
    defaults.update(overrides)
    admin = AdminUser(**defaults)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin
