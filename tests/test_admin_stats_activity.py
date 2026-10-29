"""
Panel de administración: tablero de estadísticas y el registro de actividad.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.argentina import ar_today
from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit, UserStatusAudit
from tests.conftest import create_admin, create_product, create_stock_alert


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


# ── tablero ──────────────────────────────────────────────────────────────────

def test_dashboard_cuenta_usuarios_y_productos(admin_client, db_session, auth_user):
    poco = create_product(db_session, auth_user, categoria="Lácteos", stock=1, imagen_url="http://x/1.jpg")
    create_stock_alert(db_session, auth_user, poco, days_until_stockout=2)
    create_product(db_session, auth_user, categoria="Lácteos", stock=5, vencimiento=ar_today())
    create_product(db_session, auth_user, categoria="Limpieza", stock=3, vencimiento=ar_today() - timedelta(days=2))

    data = admin_client.get("/admin/stats/").json()

    assert data["total_users"] >= 1
    assert data["active_users"] >= 1
    assert data["total_products"] == 3
    assert data["products_with_image"] == 1
    assert data["low_stock_products"] == 1  # alcanza para menos de 7 días
    assert data["expiring_soon_products"] == 2  # vence hoy y ya venció: las dos tienen una alerta crítica
    assert data["expired_products"] == 1
    assert data["total_units"] == 9
    assert data["critical_alerts"] == 3
    assert data["users_with_alerts"] == 1
    labels = {c["label"] for c in data["products_by_category"]}
    assert labels == {"Lácteos", "Limpieza"}
    assert len(data["products_per_day_last_14d"]) == 14


def test_dashboard_agrupa_sin_categoria(admin_client, db_session, auth_user):
    # La columna es NOT NULL en la base real, pero una cadena vacía es igual de
    # "sin categoría" a los fines del agrupado.
    create_product(db_session, auth_user, categoria="", stock=5)

    data = admin_client.get("/admin/stats/").json()

    labels = {c["label"] for c in data["products_by_category"]}
    assert "Sin categoría" in labels


def test_dashboard_top_usuarios_por_productos(admin_client, db_session, auth_user):
    create_product(db_session, auth_user)
    create_product(db_session, auth_user)

    data = admin_client.get("/admin/stats/").json()

    top = data["top_users_by_products"]
    assert any(u["email"] == auth_user.email and u["count"] == 2 for u in top)


def test_dashboard_requiere_admin():
    with TestClient(app) as c:
        assert c.get("/admin/stats/").status_code in (401, 403)


# ── actividad ────────────────────────────────────────────────────────────────

def test_activity_sincroniza_registros_de_usuarios_nuevos(admin_client, db_session, auth_user):
    data = admin_client.get("/admin/activity/").json()

    registrado = next(e for e in data["items"] if e["kind"] == "registered")
    assert registrado["target_email"] == auth_user.email

    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "registered").all()
    assert len(log) == 1

    # Una segunda consulta no duplica el evento de registro.
    admin_client.get("/admin/activity/")
    log_again = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "registered").all()
    assert len(log_again) == 1


def test_activity_incluye_status_y_rol_y_los_ordena_por_fecha(admin_client, db_session, auth_user):
    uid = str(auth_user.id)
    db_session.add(UserStatusAudit(user_id=uid, changed_by="jefe@x.com", is_active=False, reason="molesto"))
    db_session.add(AdminRoleAudit(user_id=uid, changed_by="jefe@x.com", action="granted"))
    db_session.add(AdminActivityLog(
        event_type="product_deleted", actor_email="jefe@x.com", target_user_id=uid,
        target_email=auth_user.email, description="borró un producto",
    ))
    db_session.commit()

    data = admin_client.get("/admin/activity/").json()

    kinds = {e["kind"] for e in data["items"]}
    assert {"status", "role", "product_deleted", "registered"}.issubset(kinds)
    status_entry = next(e for e in data["items"] if e["kind"] == "status")
    assert "deshabilitado" in status_entry["description"]
    assert "molesto" in status_entry["description"]
    role_entry = next(e for e in data["items"] if e["kind"] == "role")
    assert "recibió" in role_entry["description"]


def test_activity_se_puede_filtrar_por_tipo(admin_client, db_session, auth_user):
    uid = str(auth_user.id)
    db_session.add(UserStatusAudit(user_id=uid, changed_by="jefe@x.com", is_active=True))
    db_session.add(AdminActivityLog(
        event_type="login", actor_email=auth_user.email, target_user_id=uid,
        target_email=auth_user.email, description="inició sesión",
    ))
    db_session.commit()

    data = admin_client.get("/admin/activity/", params={"kind": "login"}).json()

    assert all(e["kind"] == "login" for e in data["items"])
    assert data["total"] == 1


def test_activity_incluye_cada_salida_de_producto_sin_las_entradas_ni_las_correcciones(admin_client, db_session, auth_user):
    from app.models.app_models import ConsumptionEvent

    product = create_product(db_session, auth_user, nombre="Yerba", stock=10)
    uid = str(auth_user.id)
    for quantity, event_type in ((1, "consumed"), (3, "sold"), (2, "restocked")):
        db_session.add(ConsumptionEvent(product_id=product.id, user_id=uid, quantity=quantity, event_type=event_type))
    original = ConsumptionEvent(product_id=product.id, user_id=uid, quantity=5, event_type="consumed")
    db_session.add(original)
    db_session.commit()
    # una corrección: el movimiento original y su reversa no cuentan, queda el corregido
    db_session.add(ConsumptionEvent(product_id=product.id, user_id=uid, quantity=5, event_type="restocked", reverses_event_id=original.id))
    db_session.add(ConsumptionEvent(product_id=product.id, user_id=uid, quantity=4, event_type="consumed"))
    db_session.commit()

    data = admin_client.get("/admin/activity/", params={"kind": "product_exit"}).json()

    assert data["total"] == 3
    assert all(e["kind"] == "product_exit" and e["target_email"] == auth_user.email for e in data["items"])
    descriptions = sorted(e["description"] for e in data["items"])
    assert descriptions == sorted([
        f'{auth_user.email} sacó 1 unidad de "Yerba" (consumido)',
        f'{auth_user.email} sacó 3 unidades de "Yerba" (vendido)',
        f'{auth_user.email} sacó 4 unidades de "Yerba" (consumido)',
    ])
    # también aparecen en la lista completa
    assert any(e["kind"] == "product_exit" for e in admin_client.get("/admin/activity/").json()["items"])


def test_las_cargas_de_stock_a_un_producto_existente_son_entradas_tambien_las_guardadas_como_edicion(admin_client, db_session, auth_user):
    uid = str(auth_user.id)
    for kind, text in (
        ("product_updated", f'{auth_user.email} ingresó 30 unidades a "Pan Lactal" (stock: 32) desde el dashboard'),
        ("product_updated", f'{auth_user.email} sumó 1 unidad a "Pan" (stock: 3) por Ticket'),
        ("product_updated", f'{auth_user.email} editó "Pan" (nombre) desde el dashboard'),
        ("product_created", f'{auth_user.email} cargó "Pan" (stock: 2) desde el dashboard'),
    ):
        db_session.add(AdminActivityLog(event_type=kind, actor_email=auth_user.email, target_user_id=uid, target_email=auth_user.email, description=text))
    db_session.commit()

    entradas = admin_client.get("/admin/activity/", params={"kind": "product_created"}).json()
    ediciones = admin_client.get("/admin/activity/", params={"kind": "product_updated"}).json()

    assert entradas["total"] == 3 and all(e["kind"] == "product_created" for e in entradas["items"])
    assert [e["description"] for e in ediciones["items"]] == [f'{auth_user.email} editó "Pan" (nombre) desde el dashboard']
    todos = admin_client.get("/admin/activity/").json()["items"]
    assert sum(1 for e in todos if e["kind"] == "product_created") == 3


def test_activity_respeta_paginacion(admin_client, db_session, auth_user):
    for i in range(3):
        db_session.add(AdminActivityLog(
            event_type="login", actor_email=f"u{i}@x.com", target_user_id=None,
            target_email=None, description=f"login {i}",
        ))
    db_session.commit()

    data = admin_client.get("/admin/activity/", params={"kind": "login", "limit": 2, "offset": 0}).json()

    assert data["total"] == 3
    assert len(data["items"]) == 2


def test_activity_requiere_admin():
    with TestClient(app) as c:
        assert c.get("/admin/activity/").status_code in (401, 403)
