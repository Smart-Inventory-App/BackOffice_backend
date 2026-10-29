"""
Casos borde de autenticación (`app/core/security.py`) y piezas sueltas que no
tienen su propio archivo: el health-check, `get_db`, y los proxies a `ml` y a
`app_backend` (alertas).
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt

from app.api import alerts as alerts_api
from app.api import ml as ml_api
from app.core import database
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from tests.conftest import create_admin


def _token(**claims) -> str:
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ── get_current_user / get_current_admin, sin overridear nada ────────────────

def test_un_token_sin_email_es_invalido(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get("/admin/auth/me", headers={"Authorization": f"Bearer {_token(sub='x')}"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401


def test_un_token_con_una_firma_distinta_es_invalido(db_session):
    otro_token = jwt.encode({"email": "x@x.com"}, "otro-secreto", algorithm=settings.JWT_ALGORITHM)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get("/admin/auth/me", headers={"Authorization": f"Bearer {otro_token}"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401


def test_un_email_sin_cuenta_activa_no_pasa_get_current_user(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/admin/auth/me",
                headers={"Authorization": f"Bearer {_token(email='nadie@x.com')}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_un_email_que_no_es_admin_no_pasa_get_current_admin(db_session, auth_user):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/admin/stats/overview",
                headers={"Authorization": f"Bearer {_token(email=auth_user.email)}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_un_admin_inactivo_no_pasa_get_current_admin(db_session):
    create_admin(db_session, email="baja@x.com", is_active=False)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/admin/stats/overview",
                headers={"Authorization": f"Bearer {_token(email='baja@x.com')}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_un_admin_activo_pasa_get_current_admin(db_session):
    create_admin(db_session, email="admin@x.com")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/admin/stats/overview",
                headers={"Authorization": f"Bearer {_token(email='admin@x.com')}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


def test_sin_header_de_autorizacion_es_401_o_403():
    with TestClient(app) as c:
        assert c.get("/admin/stats/overview").status_code in (401, 403)


# ── health ───────────────────────────────────────────────────────────────────

def test_health_no_requiere_autenticacion():
    with TestClient(app) as c:
        resp = c.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "backoffice_backend"}


# ── get_db real (en los tests normales se sobreescribe con db_session) ───────

def test_get_db_entrega_una_sesion_y_la_cierra():
    from sqlalchemy import text

    gen = database.get_db()
    db = next(gen)
    assert db.execute(text("SELECT 1")).scalar() == 1
    with pytest.raises(StopIteration):
        next(gen)


# ── proxy a ml_service ────────────────────────────────────────────────────────

@pytest.fixture()
def ml_client(db_session):
    admin = create_admin(db_session, email="jefe@x.com")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_admin] = lambda: admin
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def ml_transport(monkeypatch):
    def install(handler):
        received = []

        def record(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return handler(request)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(record)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(ml_api.httpx, "AsyncClient", FakeClient)
        return received

    return install


def test_list_models_pasa_el_token_interno(ml_client, ml_transport):
    received = ml_transport(lambda _: httpx.Response(200, json=[{"id": "v1"}]))

    data = ml_client.get("/admin/ml/models").json()

    assert data == [{"id": "v1"}]
    assert received[0].headers["x-internal-token"] == settings.INTERNAL_SERVICE_TOKEN


def test_train_model_usa_los_valores_por_defecto(ml_client, ml_transport):
    received = ml_transport(lambda _: httpx.Response(200, json={"status": "training"}))

    resp = ml_client.post("/admin/ml/models/train")

    assert resp.status_code == 200
    assert b"gbm" in received[0].content


def test_get_feature_importances(ml_client, ml_transport):
    ml_transport(lambda _: httpx.Response(200, json={"a": 0.5}))

    data = ml_client.get("/admin/ml/models/v1/feature-importances").json()

    assert data == {"a": 0.5}


def test_activate_model(ml_client, ml_transport):
    received = ml_transport(lambda _: httpx.Response(200, json={"active": True}))

    ml_client.post("/admin/ml/models/v1/activate")

    assert received[0].url.path == "/models/v1/activate"


def test_ml_service_caido_da_502(ml_client, ml_transport):
    def down(request):
        raise httpx.ConnectError("connection refused")

    ml_transport(down)

    resp = ml_client.get("/admin/ml/models")

    assert resp.status_code == 502


def test_ml_service_rechaza_con_su_propio_status(ml_client, ml_transport):
    ml_transport(lambda _: httpx.Response(404, text="no encontrado"))

    resp = ml_client.get("/admin/ml/models/inexistente/feature-importances")

    assert resp.status_code == 404


def test_ml_requiere_admin():
    with TestClient(app) as c:
        assert c.get("/admin/ml/models").status_code in (401, 403)


# ── proxy a app_backend (alertas) ─────────────────────────────────────────────

@pytest.fixture()
def alerts_transport(monkeypatch):
    def install(handler):
        received = []

        def record(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return handler(request)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(record)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(alerts_api.httpx, "AsyncClient", FakeClient)
        return received

    return install


def test_trigger_alerts_all(ml_client, alerts_transport):
    received = alerts_transport(lambda _: httpx.Response(200, json={"queued": 5}))

    data = ml_client.post("/admin/alerts/run").json()

    assert data == {"queued": 5}
    assert received[0].url.path == "/internal/alerts/run"


def test_trigger_alerts_user(ml_client, alerts_transport):
    received = alerts_transport(lambda _: httpx.Response(200, json={"queued": 1}))

    ml_client.post("/admin/alerts/run/some-user-id")

    assert received[0].url.path == "/internal/alerts/run/some-user-id"


def test_alerts_service_caido_da_502(ml_client, alerts_transport):
    def down(request):
        raise httpx.ConnectError("connection refused")

    alerts_transport(down)

    resp = ml_client.post("/admin/alerts/run")

    assert resp.status_code == 502


def test_alerts_service_responde_con_su_propio_status_de_error(ml_client, alerts_transport):
    alerts_transport(lambda _: httpx.Response(404, text="usuario no encontrado"))

    resp = ml_client.post("/admin/alerts/run/algún-user-id")

    assert resp.status_code == 404


def test_alerts_requiere_admin():
    with TestClient(app) as c:
        assert c.post("/admin/alerts/run").status_code in (401, 403)


# ── el error de otro servicio llega como texto legible ───────────────────────
# Antes se reenviaba `response.text` como `detail`: el panel mostraba
# `{"detail":"Datos insuficientes"}` con las llaves y las comillas escapadas.

def test_un_error_de_ml_llega_con_su_mensaje_legible(ml_client, ml_transport):
    ml_transport(lambda _: httpx.Response(422, json={"detail": "Datos insuficientes para entrenar"}))

    resp = ml_client.post("/admin/ml/models/train")

    assert resp.status_code == 422
    assert resp.json() == {"detail": "Datos insuficientes para entrenar"}


def test_un_error_de_ml_que_no_es_json_llega_como_texto(ml_client, ml_transport):
    ml_transport(lambda _: httpx.Response(502, text="Bad Gateway"))

    resp = ml_client.get("/admin/ml/models")

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Bad Gateway"}


def test_un_error_de_app_backend_llega_con_su_mensaje_legible(ml_client, alerts_transport):
    alerts_transport(lambda _: httpx.Response(403, json={"detail": "Forbidden"}))

    resp = ml_client.post("/admin/alerts/run")

    assert resp.status_code == 403
    assert resp.json() == {"detail": "Forbidden"}
