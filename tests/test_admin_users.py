"""
Panel de administración: listado, detalle y gestión de usuarios de la app.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import UserProfile
from app.models.audit import AdminRoleAudit, UserStatusAudit
from tests.conftest import create_admin, create_product


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


# ── listado ──────────────────────────────────────────────────────────────────

def test_list_users_incluye_al_usuario_de_prueba(admin_client, auth_user):
    data = admin_client.get("/admin/users/").json()

    emails = [u["email"] for u in data["items"]]
    assert auth_user.email in emails
    assert data["total"] >= 1


def test_list_users_filtra_por_busqueda(admin_client, db_session, auth_user):
    data = admin_client.get("/admin/users/", params={"search": "no-existe-nadie-así"}).json()

    assert data["items"] == []
    assert data["total"] == 0


def test_list_users_filtra_por_estado_activo(admin_client, db_session, auth_user):
    data = admin_client.get("/admin/users/", params={"status_filter": "active"}).json()
    assert auth_user.email in [u["email"] for u in data["items"]]

    data = admin_client.get("/admin/users/", params={"status_filter": "inactive"}).json()
    assert auth_user.email not in [u["email"] for u in data["items"]]


def test_list_users_filtra_por_admin(admin_client, db_session, auth_user):
    create_admin(db_session, email=auth_user.email, full_name="También admin")

    data = admin_client.get("/admin/users/", params={"status_filter": "admin"}).json()

    assert [u["email"] for u in data["items"]] == [auth_user.email]
    assert data["items"][0]["is_admin"] is True


def test_list_users_incluye_el_conteo_de_productos_y_el_perfil(admin_client, db_session, auth_user):
    create_product(db_session, auth_user)
    db_session.add(UserProfile(user_id=str(auth_user.id), username="gian"))
    db_session.commit()

    item = next(u for u in admin_client.get("/admin/users/").json()["items"] if u["email"] == auth_user.email)

    assert item["product_count"] == 1
    assert item["username"] == "gian"


def test_list_users_busca_por_mail_o_por_nombre_de_usuario(admin_client, db_session, auth_user):
    db_session.add(UserProfile(user_id=str(auth_user.id), username="gianluuba"))
    db_session.commit()

    por_usuario = admin_client.get("/admin/users/", params={"search": "GIANLU"}).json()
    por_mail = admin_client.get("/admin/users/", params={"search": auth_user.email[:5]}).json()
    ninguno = admin_client.get("/admin/users/", params={"search": "zzzz-nadie"}).json()

    assert auth_user.email in [u["email"] for u in por_usuario["items"]]
    assert auth_user.email in [u["email"] for u in por_mail["items"]]
    assert ninguno["total"] == 0


# ── detalle ──────────────────────────────────────────────────────────────────

def test_get_user_devuelve_sus_productos(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche")

    data = admin_client.get(f"/admin/users/{auth_user.id}").json()

    assert data["product_count"] == 1
    assert data["products"][0]["nombre"] == "Leche"
    assert data["products"][0]["owner"]["email"] == auth_user.email


def test_get_user_inexistente_da_404(admin_client):
    resp = admin_client.get("/admin/users/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404


# ── cambio de estado ─────────────────────────────────────────────────────────

def test_update_user_status_deshabilita_y_audita(admin_client, db_session, auth_user):
    resp = admin_client.patch(
        f"/admin/users/{auth_user.id}/status", json={"is_active": False, "reason": "abuso"}
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    audit = db_session.query(UserStatusAudit).filter(UserStatusAudit.user_id == str(auth_user.id)).one()
    assert audit.changed_by == "jefe@x.com"
    assert audit.reason == "abuso"


def test_update_user_status_sin_motivo_lo_deja_vacio(admin_client, db_session, auth_user):
    admin_client.patch(f"/admin/users/{auth_user.id}/status", json={"is_active": False, "reason": "   "})

    audit = db_session.query(UserStatusAudit).filter(UserStatusAudit.user_id == str(auth_user.id)).one()
    assert audit.reason is None


def test_update_user_status_inexistente_da_404(admin_client):
    resp = admin_client.patch(
        "/admin/users/00000000-0000-0000-0000-000000000000/status", json={"is_active": False}
    )

    assert resp.status_code == 404


# ── quitar rol de administrador ──────────────────────────────────────────────

# ── otorgar admin ────────────────────────────────────────────────────────────

def test_grant_admin_a_un_usuario_delega_en_auth_y_audita(admin_client, db_session, auth_user, auth_service):
    received = auth_service(
        lambda _: httpx.Response(
            201,
            json={"id": 9, "email": auth_user.email, "full_name": None, "is_active": True, "password_set": False, "created_at": None},
        )
    )

    resp = admin_client.post(
        f"/admin/users/{auth_user.id}/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 201
    assert resp.json()["is_admin"] is True
    assert received[0].url.path == "/auth/admin/admins"
    assert received[0].headers["authorization"] == "Bearer faketoken"
    audit = db_session.query(AdminRoleAudit).filter(AdminRoleAudit.action == "granted").one()
    assert audit.user_id == str(auth_user.id)


def test_grant_admin_manda_el_nombre_del_perfil(admin_client, db_session, auth_user, auth_service):
    db_session.add(UserProfile(user_id=str(auth_user.id), first_name="Ana", last_name="Gómez"))
    db_session.commit()
    received = auth_service(lambda _: httpx.Response(201, json={"id": 9, "email": auth_user.email, "full_name": "Ana Gómez", "is_active": True, "password_set": False, "created_at": None}))

    admin_client.post(f"/admin/users/{auth_user.id}/admin", headers={"Authorization": "Bearer faketoken"})

    import json
    assert json.loads(received[0].content)["full_name"] == "Ana Gómez"


def test_grant_admin_de_un_usuario_inexistente_da_404(admin_client):
    resp = admin_client.post(
        "/admin/users/00000000-0000-0000-0000-000000000000/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 404


def test_grant_admin_de_alguien_que_ya_es_admin_da_409_sin_llamar_a_auth(admin_client, db_session, auth_user, auth_service):
    create_admin(db_session, email=auth_user.email)
    received = auth_service(lambda _: httpx.Response(201, json={}))

    resp = admin_client.post(
        f"/admin/users/{auth_user.id}/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 409
    assert received == []


def test_grant_admin_rechazado_por_auth_da_409(admin_client, auth_user, auth_service):
    auth_service(lambda _: httpx.Response(409, json={"detail": "auth.admin_already_exists"}))

    resp = admin_client.post(
        f"/admin/users/{auth_user.id}/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 409


def test_remove_admin_quita_el_rol(admin_client, db_session, auth_user):
    create_admin(db_session, email=auth_user.email)
    create_admin(db_session, email="otro-admin@x.com")  # para que no sea el último

    resp = admin_client.delete(f"/admin/users/{auth_user.id}/admin")

    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False
    audit = db_session.query(AdminRoleAudit).filter(AdminRoleAudit.action == "revoked").one()
    assert audit.user_id == str(auth_user.id)


def test_remove_admin_de_un_usuario_inexistente_da_404(admin_client):
    resp = admin_client.delete("/admin/users/00000000-0000-0000-0000-000000000000/admin")

    assert resp.status_code == 404


def test_remove_admin_de_alguien_que_no_es_admin_da_404(admin_client, auth_user):
    resp = admin_client.delete(f"/admin/users/{auth_user.id}/admin")

    assert resp.status_code == 404


def test_no_te_podes_quitar_tu_propio_rol(admin_client, db_session, auth_user):
    # El admin autenticado es `jefe@x.com`; para pedir quitarse a sí mismo el rol
    # necesita también un `AuthUser` con ese email (el endpoint busca por user_id).
    from app.models.app_models import AuthUser

    propio = AuthUser(email="jefe@x.com", hashed_password="x", is_active=True)
    db_session.add(propio)
    db_session.commit()
    db_session.refresh(propio)

    resp = admin_client.delete(f"/admin/users/{propio.id}/admin")

    assert resp.status_code == 403


def test_no_se_puede_quitar_al_ultimo_administrador(admin_client, db_session, auth_user):
    from app.models.admin import AdminUser

    create_admin(db_session, email=auth_user.email)
    # `jefe@x.com` (quien hace el pedido) queda inactivo para que el único admin
    # activo sea el propio `auth_user`: así se puede probar el límite sin que la
    # cuenta que hace el pedido cuente como un segundo admin activo.
    jefe = db_session.query(AdminUser).filter(AdminUser.email == "jefe@x.com").one()
    jefe.is_active = False
    db_session.commit()

    resp = admin_client.delete(f"/admin/users/{auth_user.id}/admin")

    assert resp.status_code == 409


# ── auditoría combinada ──────────────────────────────────────────────────────

def test_get_user_audit_combina_status_y_rol_por_fecha(admin_client, db_session, auth_user):
    uid = str(auth_user.id)
    db_session.add(UserStatusAudit(user_id=uid, changed_by="jefe@x.com", is_active=False, reason="x"))
    db_session.add(AdminRoleAudit(user_id=uid, changed_by="jefe@x.com", action="granted"))
    db_session.commit()

    data = admin_client.get(f"/admin/users/{uid}/audit").json()

    kinds = {e["kind"] for e in data}
    assert kinds == {"status", "role"}


def test_los_endpoints_de_usuarios_requieren_admin():
    with TestClient(app) as c:
        assert c.get("/admin/users/").status_code in (401, 403)


# ── un id que no es un UUID es un usuario que no existe, no un error del servidor ──

@pytest.mark.parametrize("call", [
    lambda c: c.get("/admin/users/no-es-un-uuid"),
    lambda c: c.patch("/admin/users/no-es-un-uuid/status", json={"is_active": False}),
    lambda c: c.delete("/admin/users/no-es-un-uuid/admin"),
])
def test_un_id_mal_formado_da_404_y_no_500(admin_client, call):
    resp = call(admin_client)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Usuario no encontrado"


def test_grant_admin_con_un_error_inesperado_de_auth_lo_reenvia_sin_auditar(admin_client, db_session, auth_user, auth_service):
    auth_service(lambda _: httpx.Response(403, json={"detail": "auth.not_an_admin"}))

    resp = admin_client.post(
        f"/admin/users/{auth_user.id}/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "No se pudo otorgar el rol de administrador"
    assert db_session.query(AdminRoleAudit).count() == 0


def test_grant_admin_reenvia_el_bearer_del_caller_a_auth(admin_client, auth_user, auth_service):
    received = auth_service(lambda _: httpx.Response(201, json={}))

    admin_client.post(f"/admin/users/{auth_user.id}/admin", headers={"Authorization": "Bearer token-del-caller"})

    assert received[0].headers["authorization"] == "Bearer token-del-caller"
    assert received[0].url.path == "/auth/admin/admins"


def test_grant_admin_sin_authorization_es_422(admin_client, auth_user):
    resp = admin_client.post(f"/admin/users/{auth_user.id}/admin")

    assert resp.status_code == 422


def test_grant_admin_con_el_servicio_auth_caido_da_503_sin_auditar(admin_client, db_session, auth_user, auth_service):
    def down(request):
        raise httpx.ConnectError("connection refused")

    auth_service(down)

    resp = admin_client.post(
        f"/admin/users/{auth_user.id}/admin",
        headers={"Authorization": "Bearer faketoken"},
    )

    assert resp.status_code == 503
    assert db_session.query(AdminRoleAudit).count() == 0


# ── tendencias y cambios de un usuario ───────────────────────────────────────

def test_las_tendencias_de_un_usuario_son_las_de_su_dashboard(admin_client, client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "consumed"})

    data = admin_client.get(f"/admin/users/{auth_user.id}/metrics", params={"period": 7}).json()

    assert data["period_days"] == 7 and len(data["consumo_por_dia"]) == 7
    assert data["total_consumido"] == 3


def test_los_cambios_de_un_usuario_salen_del_mas_nuevo_al_mas_viejo_y_se_paginan(admin_client, client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "consumed"})
    client.post(f"/me/products/{product.id}/restock", json={"quantity": 5})

    todos = admin_client.get(f"/admin/users/{auth_user.id}/changes", params={"limit": 10}).json()
    primero = admin_client.get(f"/admin/users/{auth_user.id}/changes", params={"limit": 1}).json()
    segundo = admin_client.get(f"/admin/users/{auth_user.id}/changes", params={"limit": 1, "offset": 1}).json()

    assert [c["kind"] for c in todos] == ["entrada", "salida", "entrada"]
    assert primero == todos[:1] and segundo == todos[1:2]
    assert todos[0]["stock_before"] == 7 and todos[0]["stock_after"] == 12


def test_tendencias_y_cambios_de_un_usuario_inexistente_son_404(admin_client):
    assert admin_client.get("/admin/users/no-es-un-uuid/metrics").status_code == 404
    assert admin_client.get("/admin/users/00000000-0000-0000-0000-000000000000/changes").status_code == 404
