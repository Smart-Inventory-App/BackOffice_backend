"""
Panel de administración: listado, filtros, exportación y borrado de productos
de cualquier usuario.
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import Alert, Product
from app.models.audit import AdminActivityLog
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


def test_filter_options_lista_categorias_y_propietarios(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, categoria="Lácteos")

    data = admin_client.get("/admin/products/filters/options").json()

    assert "Lácteos" in data["categorias"]
    assert any(o["email"] == auth_user.email for o in data["owners"])


def test_filter_options_se_puede_acotar_a_un_usuario(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, categoria="Lácteos")

    data = admin_client.get(
        "/admin/products/filters/options", params={"user_id": str(auth_user.id)}
    ).json()

    assert "Lácteos" in data["categorias"]


def test_list_products_filtra_por_busqueda_categoria_y_dueno(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche entera", categoria="Lácteos")
    create_product(db_session, auth_user, nombre="Detergente", categoria="Limpieza")

    data = admin_client.get("/admin/products/", params={"search": "leche"}).json()
    assert [p["nombre"] for p in data["items"]] == ["Leche entera"]

    data = admin_client.get("/admin/products/", params={"categoria": "Limpieza"}).json()
    assert [p["nombre"] for p in data["items"]] == ["Detergente"]

    data = admin_client.get("/admin/products/", params={"user_id": str(auth_user.id)}).json()
    assert data["total"] == 2


def test_list_products_filtra_por_imagen_vencido_y_bajo_stock(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Con foto", imagen_url="http://x/1.jpg")
    create_product(db_session, auth_user, nombre="Vencido", vencimiento=date(2020, 1, 1))
    create_product(db_session, auth_user, nombre="Poco stock", stock=1)

    data = admin_client.get("/admin/products/", params={"only_with_image": True}).json()
    assert [p["nombre"] for p in data["items"]] == ["Con foto"]

    data = admin_client.get("/admin/products/", params={"vencido": True}).json()
    assert [p["nombre"] for p in data["items"]] == ["Vencido"]

    data = admin_client.get("/admin/products/", params={"vencido": False}).json()
    assert "Vencido" not in [p["nombre"] for p in data["items"]]

    data = admin_client.get("/admin/products/", params={"bajo_stock": True}).json()
    assert [p["nombre"] for p in data["items"]] == ["Poco stock"]


def test_list_products_se_puede_ordenar(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="B", stock=1)
    create_product(db_session, auth_user, nombre="A", stock=2)

    data = admin_client.get("/admin/products/", params={"sort_by": "nombre", "sort_dir": "asc"}).json()

    assert [p["nombre"] for p in data["items"]] == ["A", "B"]


def test_export_products_csv_incluye_propietario(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche")

    resp = admin_client.get("/admin/products/export", params={"format": "csv"})

    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    header, row = text.strip().splitlines()[:2]
    assert "propietario" in header.split(",")
    assert auth_user.email in row


def test_export_products_xlsx(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche")

    resp = admin_client.get("/admin/products/export", params={"format": "xlsx"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_export_products_admite_filtros_y_orden(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", categoria="Lácteos")
    create_product(db_session, auth_user, nombre="Detergente", categoria="Limpieza")

    resp = admin_client.get(
        "/admin/products/export",
        params={"format": "csv", "categoria": "Lácteos", "sort_by": "nombre"},
    )

    text = resp.content.decode("utf-8-sig")
    assert "Detergente" not in text
    assert "Leche" in text


def test_get_product_devuelve_su_dueno(admin_client, db_session, auth_user):
    producto = create_product(db_session, auth_user, nombre="Leche")

    data = admin_client.get(f"/admin/products/{producto.id}").json()

    assert data["owner"]["email"] == auth_user.email


def test_get_product_inexistente_da_404(admin_client):
    resp = admin_client.get("/admin/products/999999")

    assert resp.status_code == 404


def test_delete_product_lo_borra_y_audita(admin_client, db_session, auth_user):
    producto = create_product(db_session, auth_user, nombre="A borrar")

    resp = admin_client.delete(f"/admin/products/{producto.id}")

    assert resp.status_code == 204
    assert db_session.query(Product).filter(Product.id == producto.id).first() is None
    log = db_session.query(AdminActivityLog).filter(AdminActivityLog.event_type == "product_deleted").one()
    assert "A borrar" in log.description
    assert log.actor_email == "jefe@x.com"


def test_delete_product_inexistente_da_404(admin_client):
    resp = admin_client.delete("/admin/products/999999")

    assert resp.status_code == 404


def test_los_endpoints_de_productos_requieren_admin():
    with TestClient(app) as c:
        assert c.get("/admin/products/").status_code in (401, 403)


def test_delete_product_tambien_borra_sus_alertas(admin_client, db_session, auth_user):
    # Regresión: en Postgres la FK de alerts.product_id rechazaba el borrado (500).
    producto = create_product(db_session, auth_user, nombre="Con alerta")
    otro = create_product(db_session, auth_user, nombre="Otro")
    for p in (producto, otro):
        db_session.add(Alert(user_id=auth_user.id, product_id=p.id, type="expiry", level="warning"))
    db_session.commit()

    resp = admin_client.delete(f"/admin/products/{producto.id}")

    assert resp.status_code == 204
    assert db_session.query(Alert).filter(Alert.product_id == producto.id).count() == 0
    assert db_session.query(Alert).filter(Alert.product_id == otro.id).count() == 1
