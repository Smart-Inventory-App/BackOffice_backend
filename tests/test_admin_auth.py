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

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin, get_current_user
from app.main import app
from app.models.admin import AdminUser
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit
from tests.conftest import create_admin


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

    assert data == {"exists": True, "is_admin": True, "password_set": True, "google_only": False}


def test_check_reconoce_un_usuario_de_la_app_sin_ser_admin(db_client, db_session):
    db_session.add(AuthUser(email="user@x.com", hashed_password="x", is_active=True))
    db_session.commit()

    data = db_client.post("/admin/auth/check", json={"email": "user@x.com"}).json()

    assert data == {"exists": True, "is_admin": False, "password_set": True, "google_only": False}


def test_check_un_email_desconocido_no_existe(db_client, db_session):
    data = db_client.post("/admin/auth/check", json={"email": "nadie@x.com"}).json()

    assert data == {"exists": False, "is_admin": False, "password_set": False, "google_only": False}


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

    resp = db_client.post("/admin/auth/set-password", json={"email": "nuevo@x.com", "password": "secreto123", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 204
    assert received[0].url.path == "/auth/admin/set-password"


def test_set_password_ya_definida_es_conflicto(db_client, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.password_already_set"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "ya@x.com", "password": "x", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 409


def test_set_password_de_un_email_que_no_es_admin_es_403(db_client, auth_service):
    auth_service(lambda _: httpx.Response(403, json={"detail": "auth.not_an_admin"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "nadie@x.com", "password": "x", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 403


def test_set_password_email_ya_registrado_en_auth_se_da_por_valido(db_client, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.email_already_registered"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "repetido@x.com", "password": "x", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 409
    assert "Iniciá sesión" in resp.json()["detail"]


def test_set_password_el_servicio_auth_caido_da_503(db_client, auth_service):
    def down(request):
        raise httpx.ConnectError("connection refused")

    auth_service(down)

    resp = db_client.post("/admin/auth/set-password", json={"email": "caido@x.com", "password": "x", "invite_token": "codigo-de-invitacion"})

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
    # el nombre de la sesión es el del perfil, no el que se cargó al dar de alta al administrador
    assert data["full_name"] is None


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


def test_create_admin_de_un_email_sin_cuenta_pide_una_contrasena_inicial(admin_client, auth_service):
    received = auth_service(lambda _: httpx.Response(201, json={}))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "flamante@x.com", "full_name": "Flamante"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 404
    assert "contraseña inicial" in resp.json()["detail"]
    assert received == []  # sin la contraseña no se le pide nada a auth


def test_create_admin_de_un_email_sin_cuenta_con_contrasena_inicial_crea_la_cuenta_en_auth(admin_client, auth_service):
    import json as jsonlib

    received = auth_service(
        lambda _: httpx.Response(
            201,
            json={"id": 2, "email": "flamante@x.com", "full_name": "Flamante", "is_active": True, "password_set": True, "created_at": None},
        )
    )

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "flamante@x.com", "full_name": "Flamante", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201
    assert resp.json()["password_set"] is True and resp.json()["account_created"] is True
    assert received[0].url.path == "/auth/admin/admins"
    assert received[0].headers["authorization"] == "Bearer faketoken"
    assert jsonlib.loads(received[0].content)["initial_password"] == "Inicial.2026"


def test_una_contrasena_inicial_que_auth_rechaza_se_explica(admin_client, auth_service):
    auth_service(lambda _: httpx.Response(400, json={"detail": "auth.password_missing_uppercase"}))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "flamante@x.com", "initial_password": "inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 400 and resp.json()["detail"] == "La contraseña debe incluir al menos una mayúscula."


def test_una_respuesta_400_de_auth_sin_cuerpo_usa_el_mensaje_generico(admin_client, auth_service):
    auth_service(lambda _: httpx.Response(400, text="mal"))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "flamante@x.com", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.json()["detail"] == "La contraseña inicial no es válida."


def test_create_admin_de_un_usuario_con_cuenta_no_manda_la_contrasena_inicial(admin_client, db_session, auth_service):
    import json as jsonlib

    db_session.add(AuthUser(email="conta@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    received = auth_service(
        lambda _: httpx.Response(
            201, json={"id": 5, "email": "conta@x.com", "full_name": None, "is_active": True, "password_set": True, "created_at": None}
        )
    )

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "conta@x.com", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201 and resp.json()["account_created"] is False
    assert "initial_password" not in jsonlib.loads(received[0].content)


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
        json={"email": "repe@x.com", "initial_password": "Inicial.2026"},
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


def test_set_password_con_un_error_de_auth_lo_reenvia(db_client, auth_service):
    auth_service(lambda _: httpx.Response(400, json={"detail": "auth.password_too_short"}))

    resp = db_client.post("/admin/auth/set-password", json={"email": "x@x.com", "password": "corta", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "auth.password_too_short"


def test_set_password_con_un_error_de_auth_sin_cuerpo_usa_un_mensaje_generico(db_client, auth_service):
    auth_service(lambda _: httpx.Response(500, text="boom"))

    resp = db_client.post("/admin/auth/set-password", json={"email": "x@x.com", "password": "x", "invite_token": "codigo-de-invitacion"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "No se pudo definir la contraseña"


def test_create_admin_con_un_error_inesperado_de_auth_lo_reenvia_sin_crear_nada(admin_client, db_session, auth_service):
    auth_service(lambda _: httpx.Response(403, json={"detail": "auth.not_an_admin"}))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "nuevo@x.com", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "No se pudo crear el administrador"
    assert db_session.query(AdminRoleAudit).count() == 0


def test_create_admin_reenvia_el_bearer_del_caller_a_auth(admin_client, auth_service):
    received = auth_service(lambda _: httpx.Response(201, json={
        "id": 1, "email": "nuevo@x.com", "full_name": None, "is_active": True, "password_set": False, "created_at": None,
    }))

    admin_client.post(
        "/admin/auth/admins",
        json={"email": "nuevo@x.com", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer token-del-caller"},
    )

    assert received[0].headers["authorization"] == "Bearer token-del-caller"
    assert received[0].url.path == "/auth/admin/admins"


def test_register_con_una_respuesta_de_error_sin_json_usa_un_mensaje_generico(db_client, auth_service):
    auth_service(lambda _: httpx.Response(502, text="bad gateway"))

    resp = db_client.post("/admin/auth/register", json={"email": "n@x.com", "password": "Abcdefg1"})

    assert resp.status_code == 502
    assert resp.json()["detail"] == "No se pudo crear la cuenta"


def test_register_no_crea_nada_en_la_base_del_backoffice(db_client, db_session, auth_service):
    auth_service(lambda _: httpx.Response(201, json={}))

    db_client.post("/admin/auth/register", json={"email": "n@x.com", "password": "Abcdefg1"})

    assert db_session.query(AdminUser).filter(AdminUser.email == "n@x.com").count() == 0


# ── set-password: el código de invitación viaja a `auth` ──────────────────────

def test_set_password_reenvia_el_codigo_de_invitacion_a_auth(db_client, auth_service):
    import json

    received = auth_service(lambda _: httpx.Response(204))

    db_client.post(
        "/admin/auth/set-password",
        json={"email": "nuevo@x.com", "password": "Abcdefg1", "invite_token": "el-codigo"},
    )

    assert json.loads(received[0].content) == {"email": "nuevo@x.com", "password": "Abcdefg1", "invite_token": "el-codigo"}


def test_set_password_sin_codigo_de_invitacion_es_422_y_no_llama_a_auth(db_client, auth_service):
    received = auth_service(lambda _: httpx.Response(204))

    resp = db_client.post("/admin/auth/set-password", json={"email": "nuevo@x.com", "password": "Abcdefg1"})

    assert resp.status_code == 422
    assert received == []


def test_set_password_con_un_codigo_invalido_da_un_mensaje_claro(db_client, auth_service):
    auth_service(lambda _: httpx.Response(403, json={"detail": "auth.invalid_invite"}))

    resp = db_client.post(
        "/admin/auth/set-password",
        json={"email": "nuevo@x.com", "password": "Abcdefg1", "invite_token": "vencido"},
    )

    assert resp.status_code == 403
    assert "código de invitación" in resp.json()["detail"]
    assert "auth.invalid_invite" not in resp.json()["detail"]


# ── alta de admin: el código se le entrega a quien da de alta ─────────────────

def test_create_admin_devuelve_el_codigo_de_invitacion_que_entrego_auth(admin_client, auth_service):
    auth_service(lambda _: httpx.Response(201, json={
        "id": 1, "email": "nuevo@x.com", "full_name": None, "is_active": True,
        "password_set": False, "created_at": None, "invite_token": "codigo-de-auth",
    }))

    resp = admin_client.post(
        "/admin/auth/admins",
        json={"email": "nuevo@x.com", "initial_password": "Inicial.2026"},
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201
    assert resp.json()["invite_token"] == "codigo-de-auth"


def test_create_admin_de_un_usuario_existente_no_trae_codigo(admin_client, db_session, auth_service):
    db_session.add(AuthUser(email="ya@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(201, json={
        "id": 2, "email": "ya@x.com", "full_name": None, "is_active": True,
        "password_set": True, "created_at": None, "invite_token": None,
    }))

    resp = admin_client.post(
        "/admin/auth/admins", json={"email": "ya@x.com"}, headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201
    assert resp.json()["invite_token"] is None


def test_el_listado_de_admins_nunca_trae_codigos(admin_client, db_session):
    create_admin(db_session, email="otro@x.com", password_set=False)

    resp = admin_client.get("/admin/auth/admins")

    assert resp.status_code == 200
    assert all(a["invite_token"] is None for a in resp.json())


def test_check_avisa_cuando_la_cuenta_se_creo_con_google_y_no_tiene_contrasena(db_client, db_session):
    db_session.add(AuthUser(email="google@x.com", hashed_password="x", is_active=True, has_password=False))
    db_session.commit()

    data = db_client.post("/admin/auth/check", json={"email": "google@x.com"}).json()

    assert data == {"exists": True, "is_admin": False, "password_set": False, "google_only": True}


# ── recuperar la contraseña (delega en `auth`) ────────────────────────────────

def test_el_codigo_de_recuperacion_se_pide_a_auth(db_client, auth_service):
    recibidas = auth_service(lambda request: httpx.Response(200, json={"detail": "auth.reset_code_sent"}))

    resp = db_client.post("/admin/auth/forgot-password", json={"email": "user@x.com"})

    assert resp.status_code == 200 and resp.json() == {"detail": "sent"}
    assert recibidas[0].url.path == "/auth/forgot-password"


def test_si_auth_falla_al_pedir_el_codigo_se_explica(db_client, auth_service):
    auth_service(lambda request: httpx.Response(500, json={"detail": "boom"}))

    resp = db_client.post("/admin/auth/forgot-password", json={"email": "user@x.com"})

    assert resp.status_code == 500 and resp.json()["detail"] == "No se pudo enviar el código."


def test_el_codigo_correcto_devuelve_el_permiso_para_cambiar_la_contrasena(db_client, auth_service):
    recibidas = auth_service(lambda request: httpx.Response(200, json={"reset_token": "abc"}))

    resp = db_client.post("/admin/auth/verify-code", json={"email": "user@x.com", "code": "123456"})

    assert resp.json() == {"reset_token": "abc"}
    assert recibidas[0].url.path == "/auth/verify-code"


@pytest.mark.parametrize(
    "detail, status_code, mensaje",
    [
        ("auth.invalid_or_expired_code", 400, "El código no es válido o venció. Pedí uno nuevo."),
        ("auth.too_many_attempts", 429, "Demasiados intentos. Pedí un código nuevo."),
        ("auth.otra_cosa", 400, "No se pudo verificar el código."),
    ],
)
def test_un_codigo_incorrecto_se_explica_en_castellano(db_client, auth_service, detail, status_code, mensaje):
    auth_service(lambda request: httpx.Response(status_code, json={"detail": detail}))

    resp = db_client.post("/admin/auth/verify-code", json={"email": "user@x.com", "code": "000000"})

    assert resp.status_code == status_code and resp.json()["detail"] == mensaje


def test_la_contrasena_nueva_se_guarda_en_auth(db_client, auth_service):
    recibidas = auth_service(lambda request: httpx.Response(200, json=None))

    resp = db_client.post("/admin/auth/reset-password", json={"reset_token": "abc", "new_password": "Nueva1234"})

    assert resp.status_code == 204
    assert recibidas[0].url.path == "/auth/reset-password"


def test_una_contrasena_igual_a_la_actual_o_corta_se_explica(db_client, auth_service):
    auth_service(lambda request: httpx.Response(400, json={"detail": "auth.password_same_as_current"}))

    resp = db_client.post("/admin/auth/reset-password", json={"reset_token": "abc", "new_password": "Misma1234"})

    assert resp.status_code == 400 and resp.json()["detail"] == "La contraseña nueva no puede ser igual a la actual."


def test_un_error_sin_cuerpo_json_usa_el_mensaje_por_defecto(db_client, auth_service):
    auth_service(lambda request: httpx.Response(502, text="Bad gateway"))

    resp = db_client.post("/admin/auth/reset-password", json={"reset_token": "abc", "new_password": "Nueva1234"})

    assert resp.status_code == 502 and resp.json()["detail"] == "No se pudo cambiar la contraseña."


# ── cambiar la contraseña (contraseña inicial de un administrador) ────────────

@pytest.fixture()
def user_client(db_session, auth_user):
    from app.core.database import get_db
    from app.core.security import get_current_user
    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: auth_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_me_avisa_que_la_cuenta_tiene_que_cambiar_la_contrasena(user_client, db_session, auth_user):
    assert user_client.get("/admin/auth/me").json()["must_change_password"] is False

    auth_user.must_change_password = True
    db_session.commit()

    assert user_client.get("/admin/auth/me").json()["must_change_password"] is True


def test_cambiar_la_contrasena_se_la_pide_a_auth_con_el_mismo_token(user_client, auth_service):
    import json as jsonlib

    received = auth_service(lambda _: httpx.Response(204))

    resp = user_client.post(
        "/admin/auth/change-password",
        json={"current_password": "Inicial.2026", "new_password": "Definitiva.99"},
        headers={"Authorization": "Bearer tok-usuario"},
    )

    assert resp.status_code == 204
    assert received[0].url.path == "/auth/change-password"
    assert received[0].headers["authorization"] == "Bearer tok-usuario"
    assert jsonlib.loads(received[0].content) == {"current_password": "Inicial.2026", "new_password": "Definitiva.99"}


@pytest.mark.parametrize(
    "detail, message",
    [
        ("auth.invalid_credentials", "La contraseña actual no es correcta."),
        ("auth.password_same_as_current", "La contraseña nueva no puede ser igual a la actual."),
        ("auth.password_too_short", "La contraseña debe tener al menos 8 caracteres."),
        ("auth.algo_raro", "No se pudo cambiar la contraseña."),
    ],
)
def test_un_cambio_rechazado_se_explica_en_castellano(user_client, auth_service, detail, message):
    auth_service(lambda _: httpx.Response(400, json={"detail": detail}))

    resp = user_client.post(
        "/admin/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
        headers={"Authorization": "Bearer tok"},
    )

    assert resp.status_code == 400 and resp.json()["detail"] == message


def test_un_cambio_rechazado_sin_cuerpo_json_usa_el_mensaje_generico(user_client, auth_service):
    auth_service(lambda _: httpx.Response(500, text="boom"))

    resp = user_client.post(
        "/admin/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
        headers={"Authorization": "Bearer tok"},
    )

    assert resp.status_code == 500 and resp.json()["detail"] == "No se pudo cambiar la contraseña."


# ── ingreso y registro con Google (delega en `auth`) ──────────────────────────

def google_token(email, secret=None):
    from datetime import datetime, timedelta

    from jose import jwt

    from app.core.config import settings

    return jwt.encode(
        {"sub": "u", "email": email, "exp": datetime.utcnow() + timedelta(minutes=5)},
        secret or settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def test_google_entra_con_el_token_que_devuelve_auth_y_lo_deja_en_la_actividad(db_client, db_session, auth_service):
    import json as jsonlib

    token = google_token("ana@x.com")
    received = auth_service(lambda _: httpx.Response(200, json={"access_token": token, "token_type": "bearer"}))

    resp = db_client.post("/admin/auth/google", json={"id_token": "id-token-de-google"})

    assert resp.status_code == 200 and resp.json()["access_token"] == token
    assert received[0].url.path == "/auth/google"
    assert jsonlib.loads(received[0].content) == {"id_token": "id-token-de-google"}
    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "login").one()
    assert log.actor_email == "ana@x.com" and "con Google en el dashboard" in log.description
    assert "(administrador)" not in log.description


def test_google_de_un_administrador_lo_marca_en_la_actividad(db_client, db_session, auth_service):
    create_admin(db_session, email="jefe@x.com", password_set=True)
    db_session.add(AuthUser(email="jefe@x.com", hashed_password="x", is_active=True))
    db_session.commit()
    auth_service(lambda _: httpx.Response(200, json={"access_token": google_token("jefe@x.com")}))

    assert db_client.post("/admin/auth/google", json={"id_token": "t"}).status_code == 200

    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "login").one()
    assert "(administrador)" in log.description and log.target_user_id is not None


def test_google_no_deja_entrar_a_un_administrador_dado_de_baja(db_client, db_session, auth_service):
    create_admin(db_session, email="baja@x.com", is_active=False)
    auth_service(lambda _: httpx.Response(200, json={"access_token": google_token("baja@x.com")}))

    resp = db_client.post("/admin/auth/google", json={"id_token": "t"})

    assert resp.status_code == 403 and resp.json()["detail"] == "Cuenta deshabilitada"


@pytest.mark.parametrize(
    "status_code, detail, message",
    [
        (401, "auth.google_invalid_token", "Token de Google inválido"),
        (401, "auth.google_email_not_verified", "El email de Google no está verificado"),
        (503, "auth.google_unreachable", "No se pudo contactar a Google"),
        (503, "auth.google_not_configured", "El inicio de sesión con Google no está disponible por el momento"),
        (403, "auth.account_disabled", "La cuenta está desactivada"),
        (500, "auth.algo_raro", "Error al iniciar sesión con Google"),
    ],
)
def test_google_explica_cada_error_con_los_mensajes_de_la_app(db_client, auth_service, status_code, detail, message):
    auth_service(lambda _: httpx.Response(status_code, json={"detail": detail}))

    resp = db_client.post("/admin/auth/google", json={"id_token": "t"})

    assert resp.status_code == status_code and resp.json()["detail"] == message


def test_google_con_un_error_de_auth_sin_cuerpo_json_usa_el_mensaje_generico(db_client, auth_service):
    auth_service(lambda _: httpx.Response(502, text="bad gateway"))

    resp = db_client.post("/admin/auth/google", json={"id_token": "t"})

    assert resp.status_code == 502 and resp.json()["detail"] == "Error al iniciar sesión con Google"


def test_google_con_un_token_que_no_es_nuestro_se_rechaza(db_client, auth_service):
    auth_service(lambda _: httpx.Response(200, json={"access_token": "esto-no-es-un-jwt"}))

    resp = db_client.post("/admin/auth/google", json={"id_token": "t"})

    assert resp.status_code == 502


# ── el nombre de la sesión: solo el nombre y el apellido del perfil, igual para administradores y usuarios ──

def _me(db_session, email):
    from app.core.database import get_db
    from app.core.security import get_current_user
    from app.main import app

    user = db_session.query(AuthUser).filter(AuthUser.email == email).one()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with TestClient(app) as c:
            return c.get("/admin/auth/me").json()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("first, last, expected", [("Ana", "Gómez", "Ana Gómez"), ("Ana", None, "Ana"), (None, "Gómez", "Gómez"), (None, None, None)])
def test_la_sesion_muestra_el_nombre_y_el_apellido_del_perfil_si_existen(db_session, first, last, expected):
    from app.models.app_models import UserProfile

    user = AuthUser(email="ana@x.com", hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.add(UserProfile(user_id=str(user.id), first_name=first, last_name=last))
    db_session.commit()

    assert _me(db_session, "ana@x.com")["full_name"] == expected


def test_un_administrador_muestra_lo_mismo_que_un_usuario_y_no_el_nombre_del_alta(db_session):
    from app.models.app_models import UserProfile

    create_admin(db_session, email="jefe@x.com", full_name="Nombre del alta")
    user = AuthUser(email="jefe@x.com", hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.add(UserProfile(user_id=str(user.id), first_name="Jefa", last_name="Suprema"))
    db_session.commit()

    me = _me(db_session, "jefe@x.com")

    assert me["is_admin"] is True and me["full_name"] == "Jefa Suprema"


def test_una_cuenta_sin_perfil_no_muestra_nombre(db_session):
    db_session.add(AuthUser(email="sinperfil@x.com", hashed_password="x", is_active=True))
    db_session.commit()

    assert _me(db_session, "sinperfil@x.com")["full_name"] is None
