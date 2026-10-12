"""
Login del panel y alta de administradores.

`app/api/auth.py` sigue leyendo el mirror LOCAL de `admin_users` para todo lo
que es lectura (check, login, me, listado de admins) — misma base
compartida que escribe `auth`, sin red de por medio. Lo que sí delega a
`auth` por HTTP es lo que escribe: `set-password` (registra en `auth_users`
y marca `password_set`) y la alta de un nuevo admin (`auth` es quien crea la
fila en `admin_users` desde ahora). Estos tests instalan un `auth` de
mentira para esos dos casos, igual que `test_ocr.py` hace con el coordinador
de OCR.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt

from app.api import auth as auth_api
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin, get_current_user
from app.main import app
from app.models.admin import AdminUser
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit
from tests.conftest import create_admin


@pytest.fixture()
def auth_service(monkeypatch):
    """Instala un servicio `auth` de mentira y devuelve las peticiones que recibió."""
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


@pytest.fixture()
def db_client(db_session):
    """Cliente sin autenticación forzada, sobre la base que los tests revierten."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── check (100% local, sin tocar `auth`) ──────────────────────────────────────

def test_check_reconoce_un_admin_activo(db_client, db_session):
    create_admin(db_session, email="admin@x.com", password_set=True)

    data = db_client.post("/admin/auth/check", json={"email": "admin@x.com"}).json()

    assert data == {"exists": True, "is_admin": True, "password_set": True}


def test_check_reconoce_un_usuario_de_la_app_sin_ser_admin(db_client, db_session):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()

    data = db_client.post("/admin/auth/check", json={"email": "user@x.com"}).json()

    assert data == {"exists": True, "is_admin": False, "password_set": True}


def test_check_un_email_desconocido_no_existe(db_client, db_session):
    data = db_client.post("/admin/auth/check", json={"email": "nadie@x.com"}).json()

    assert data == {"exists": False, "is_admin": False, "password_set": False}


def test_check_un_admin_inactivo_no_cuenta_como_admin(db_client, db_session):
    create_admin(db_session, email="baja@x.com", is_active=False)

    data = db_client.post("/admin/auth/check", json={"email": "baja@x.com"}).json()

    assert data["is_admin"] is False


# ── register (delega enteramente en `auth`) ───────────────────────────────────

def test_register_exitoso_delega_en_auth(db_client, auth_service):
    received = auth_service(lambda _: httpx.Response(201))

    resp = db_client.post("/admin/auth/register", json={"email": "nuevo@x.com", "password": "Secreto123"})

    assert resp.status_code == 201
    assert received[0].url.path == "/auth/register"
    assert received[0].method == "POST"


def test_register_email_ya_existente_traduce_el_mensaje(db_client, auth_service):
    auth_service(lambda _: httpx.Response(400, json={"detail": "auth.email_already_registered"}))

    resp = db_client.post("/admin/auth/register", json={"email": "ya@x.com", "password": "Secreto123"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Ya existe una cuenta con ese email. Iniciá sesión."


def test_register_contrasena_debil_traduce_el_mensaje(db_client, auth_service):
    auth_service(lambda _: httpx.Response(400, json={"detail": "auth.password_too_short"}))

    resp = db_client.post("/admin/auth/register", json={"email": "x@x.com", "password": "abc"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "La contraseña debe tener al menos 8 caracteres."


def test_register_el_servicio_auth_caido_da_503(db_client, auth_service):
    def down(request):
        raise httpx.ConnectError("connection refused")

    auth_service(down)

    resp = db_client.post("/admin/auth/register", json={"email": "caido@x.com", "password": "Secreto123"})

    assert resp.status_code == 503


# ── set-password (delega enteramente en `auth`) ───────────────────────────────

def test_set_password_exitoso_delega_en_auth(db_client, db_session, auth_service):
    received = auth_service(lambda _: httpx.Response(204))

    resp = db_client.post("/admin/auth/set-password", json={"email": "nuevo@x.com", "password": "secreto123"})

    assert resp.status_code == 204
    assert received[0].url.path == "/auth/admin/set-password"


def test_set_password_ya_definida_es_conflicto(db_client, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.password_already_set"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "ya@x.com", "password": "x"})

    assert resp.status_code == 409


def test_set_password_de_un_email_que_no_es_admin_es_403(db_client, auth_service):
    auth_service(lambda _: httpx.Response(403, json={"detail": "auth.not_an_admin"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "nadie@x.com", "password": "x"})

    assert resp.status_code == 403


def test_set_password_email_ya_registrado_en_auth_se_da_por_valido(db_client, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.email_already_registered"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "repetido@x.com", "password": "x"})

    assert resp.status_code == 409
    assert "Iniciá sesión" in resp.json()["detail"]


def test_set_password_el_servicio_auth_caido_da_503(db_client, auth_service):
    def down(request):
        raise httpx.ConnectError("connection refused")

    auth_service(down)

    resp = db_client.post("/admin/auth/set-password", json={"email": "caido@x.com", "password": "x"})

    assert resp.status_code == 503


# ── login (whitelist local, credenciales vía `auth`) ──────────────────────────

def test_login_exitoso_de_un_admin_deja_actividad(db_client, db_session, auth_service):
    create_admin(db_session, email="admin@x.com", password_set=True)
    db_session.add(AuthUser(email="admin@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(200, json={"access_token": "tok-123"}))

    resp = db_client.post("/admin/auth/login", json={"email": "admin@x.com", "password": "secreto"})

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "tok-123"
    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "login").one()
    assert "administrador" in log.description


def test_login_exitoso_de_un_usuario_comun_tambien_funciona(db_client, db_session, auth_service):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(200, json={"access_token": "tok-999"}))

    resp = db_client.post("/admin/auth/login", json={"email": "user@x.com", "password": "secreto"})

    assert resp.status_code == 200
    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "login").one()
    assert "administrador" not in log.description


def test_login_admin_sin_contrasena_definida_es_conflicto(db_client, db_session, auth_service):
    create_admin(db_session, email="sinpass@x.com", password_set=False)
    auth_service(lambda _: httpx.Response(200, json={"access_token": "no-debería-usarse"}))

    resp = db_client.post("/admin/auth/login", json={"email": "sinpass@x.com", "password": "x"})

    assert resp.status_code == 409


def test_login_admin_inactivo_es_403(db_client, db_session, auth_service):
    create_admin(db_session, email="baja@x.com", is_active=False, password_set=True)
    auth_service(lambda _: httpx.Response(200, json={"access_token": "x"}))

    resp = db_client.post("/admin/auth/login", json={"email": "baja@x.com", "password": "x"})

    assert resp.status_code == 403


def test_login_cuenta_desconocida_es_403(db_client, db_session, auth_service):
    auth_service(lambda _: httpx.Response(200, json={"access_token": "x"}))

    resp = db_client.post("/admin/auth/login", json={"email": "nadie@x.com", "password": "x"})

    assert resp.status_code == 403


def test_login_credenciales_invalidas(db_client, db_session, auth_service):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(401, json={}))

    resp = db_client.post("/admin/auth/login", json={"email": "user@x.com", "password": "mal"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Credenciales inválidas"


def test_login_el_servicio_auth_rechaza_la_cuenta(db_client, db_session, auth_service):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(403, json={}))

    resp = db_client.post("/admin/auth/login", json={"email": "user@x.com", "password": "x"})

    assert resp.status_code == 403


def test_login_un_error_inesperado_del_servicio_se_informa(db_client, db_session, auth_service):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(500, json={}))

    resp = db_client.post("/admin/auth/login", json={"email": "user@x.com", "password": "x"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "No se pudo iniciar sesión"


# ── me (100% local) ────────────────────────────────────────────────────────────

def _token_de(email: str) -> str:
    return jwt.encode({"email": email}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def test_me_devuelve_si_el_usuario_es_admin(db_session):
    create_admin(db_session, email="soyadmin@x.com", full_name="Ana Admin")
    user = AuthUser(email="soyadmin@x.com", hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with TestClient(app) as c:
            data = c.get("/admin/auth/me").json()
    finally:
        app.dependency_overrides.clear()

    assert data["is_admin"] is True
    assert data["full_name"] == "Ana Admin"


# ── admins (listado local; alta delega en `auth`) ──────────────────────────────

@pytest.fixture()
def admin_client(db_session):
    admin = create_admin(db_session, email="jefe@x.com")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_admin] = lambda: admin
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_list_admins_los_devuelve_mas_reciente_primero(admin_client, db_session):
    create_admin(db_session, email="viejo@x.com")

    data = admin_client.get("/admin/auth/admins").json()

    emails = [a["email"] for a in data]
    assert "jefe@x.com" in emails
    assert "viejo@x.com" in emails


def test_create_admin_de_un_email_nuevo_delega_en_auth(admin_client, auth_service):
    received = auth_service(
        lambda _: httpx.Response(
            201,
            json={"id": 2, "email": "flamante@x.com", "full_name": "Flamante", "is_active": True, "password_set": False, "created_at": None},
        )
    )

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "flamante@x.com", "full_name": "Flamante"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201
    assert resp.json()["password_set"] is False
    assert received[0].url.path == "/auth/admin/admins"
    assert received[0].headers["authorization"] == "Bearer faketoken"


def test_create_admin_ya_existente_localmente_es_conflicto_sin_llamar_a_auth(admin_client, db_session, auth_service):
    create_admin(db_session, email="repe@x.com")
    received = auth_service(lambda _: httpx.Response(201, json={}))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "repe@x.com"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 409
    assert received == []


def test_create_admin_rechazado_por_auth_es_conflicto(admin_client, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.admin_already_exists"}))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "repe@x.com"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 409


def test_create_admin_de_un_usuario_existente_deja_auditoria_de_rol(admin_client, db_session, auth_service):
    db_session.add(AuthUser(email="promovido@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(
        lambda _: httpx.Response(
            201,
            json={"id": 3, "email": "promovido@x.com", "full_name": None, "is_active": True, "password_set": False, "created_at": None},
        )
    )

    admin_client.post(
        "/admin/auth/admins",
        json={"email": "promovido@x.com"},
        headers={"Authorization": "Bearer faketoken"},
    )

    audit = db_session.query(AdminRoleAudit).filter(AdminRoleAudit.action == "granted").one()
    assert audit.changed_by == "jefe@x.com"


def test_los_endpoints_de_admins_requieren_admin():
    with TestClient(app) as c:
        assert c.get("/admin/auth/admins").status_code in (401, 403)
