import os
import uuid

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://test:test@localhost:5432/tpp_test"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.security import get_current_user
from app.main import app
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
