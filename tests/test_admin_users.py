"""
Panel de administración: listado, detalle y gestión de usuarios de la app.
"""
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
