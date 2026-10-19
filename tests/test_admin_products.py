"""
Panel de administración: listado, filtros, exportación y borrado de productos
de cualquier usuario.
"""
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import Alert, AuthUser, Product
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


# ── orden alfabético y páginas estables ───────────────────────────────────────

def nombres(client, **params):
    return [p["nombre"] for p in client.get("/admin/products/", params=params).json()["items"]]


def test_ordenar_por_nombre_no_distingue_mayusculas_ni_tildes(admin_client, db_session, auth_user):
    for nombre in ["zanahoria", "Ñoquis", "Álvaro", "banana", "Arroz", "nata"]:
        create_product(db_session, auth_user, nombre=nombre)

    assert nombres(admin_client, sort_by="nombre") == ["Álvaro", "Arroz", "banana", "nata", "Ñoquis", "zanahoria"]
    assert nombres(admin_client, sort_by="nombre", sort_dir="desc") == [
        "zanahoria", "Ñoquis", "nata", "banana", "Arroz", "Álvaro",
    ]


def test_ordenar_por_vencimiento_deja_afuera_los_que_no_vencen(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Sin fecha")
    create_product(db_session, auth_user, nombre="Tarde", vencimiento=date(2027, 5, 1))
    create_product(db_session, auth_user, nombre="Pronto", vencimiento=date(2027, 1, 1))

    data = admin_client.get("/admin/products/", params={"sort_by": "vencimiento"}).json()

    assert [p["nombre"] for p in data["items"]] == ["Pronto", "Tarde"]
    assert data["total"] == 2
    desc = nombres(admin_client, sort_by="vencimiento", sort_dir="desc")
    assert desc == ["Tarde", "Pronto"]


@pytest.mark.parametrize("sort_by", [None, "nombre", "stock", "categoria", "vencimiento"])
def test_las_paginas_suman_toda_la_lista_sin_repetir(admin_client, db_session, auth_user, sort_by):
    for i in range(12):
        create_product(
            db_session, auth_user, nombre=f"P{i:02d}", categoria="Igual", stock=i % 3, vencimiento=date(2027, 1, 1 + i % 4)
        )
    params = {"sort_by": sort_by} if sort_by else {}
    todo = [p["id"] for p in admin_client.get("/admin/products/", params={**params, "limit": 200}).json()["items"]]

    paginas = []
    for offset in range(0, 12, 5):
        paginas += [
            p["id"]
            for p in admin_client.get("/admin/products/", params={**params, "limit": 5, "offset": offset}).json()["items"]
        ]

    assert paginas == todo
    assert len(set(paginas)) == 12


def test_el_export_sale_en_el_mismo_orden_que_el_listado(admin_client, db_session, auth_user):
    for nombre in ["banana", "Álvaro", "Arroz"]:
        create_product(db_session, auth_user, nombre=nombre)

    csv = admin_client.get("/admin/products/export", params={"format": "csv", "sort_by": "nombre"}).text

    assert csv.index("Álvaro") < csv.index("Arroz") < csv.index("banana")


# ── stock total de un mismo producto (mismo nombre y categoría) ───────────────

def otro_usuario(db_session, email="otro@x.com"):
    user = AuthUser(id=uuid.uuid4(), email=email, hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()
    return user


def test_buscar_grupos_suma_el_stock_de_todas_las_marcas(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", marca="Sancor", categoria="Lácteos", stock=3)
    create_product(db_session, auth_user, nombre="leche ", marca="La Serenísima", categoria="lácteos", stock=4,
                   vencimiento=date(2027, 3, 1))
    create_product(db_session, auth_user, nombre="Leche", categoria="Bebidas", stock=9)

    data = admin_client.get("/admin/products/groups", params={"q": "LECHE"}).json()

    assert [(g["categoria"], g["stock_total"], g["lotes"]) for g in data] == [("Bebidas", 9, 1), ("Lácteos", 7, 2)]
    assert data[1]["proximo_vencimiento"] == "2027-03-01"


def test_buscar_grupos_sin_usuario_junta_a_toda_la_plataforma(admin_client, db_session, auth_user):
    otro = otro_usuario(db_session)
    create_product(db_session, auth_user, nombre="Arroz", categoria="Almacén", stock=2)
    create_product(db_session, otro, nombre="Arroz", categoria="Almacén", stock=5)

    todos = admin_client.get("/admin/products/groups", params={"q": "arr"}).json()
    solo_uno = admin_client.get("/admin/products/groups", params={"q": "arr", "user_id": str(otro.id)}).json()

    assert (todos[0]["stock_total"], todos[0]["usuarios"]) == (7, 2)
    assert (solo_uno[0]["stock_total"], solo_uno[0]["usuarios"]) == (5, 1)


def test_buscar_grupos_sin_coincidencias_o_con_busqueda_en_blanco(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Arroz")

    assert admin_client.get("/admin/products/groups", params={"q": "zzz"}).json() == []
    assert admin_client.get("/admin/products/groups", params={"q": "   "}).json() == []
    assert admin_client.get("/admin/products/groups").status_code == 422


def test_buscar_grupos_devuelve_como_mucho_diez(admin_client, db_session, auth_user):
    for i in range(12):
        create_product(db_session, auth_user, nombre=f"Producto {i:02d}", categoria="Cat")

    assert len(admin_client.get("/admin/products/groups", params={"q": "producto"}).json()) == 10


def test_detalle_del_grupo_trae_los_productos_ordenados_por_vencimiento(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", marca="A", categoria="Lácteos", stock=1)
    create_product(db_session, auth_user, nombre="Leche", marca="B", categoria="Lácteos", stock=2, vencimiento=date(2027, 6, 1))
    create_product(db_session, auth_user, nombre="Leche", marca="C", categoria="Lácteos", stock=3, vencimiento=date(2027, 2, 1))
    create_product(db_session, auth_user, nombre="Leche", categoria="Otra", stock=50)

    data = admin_client.get("/admin/products/group", params={"nombre": "leche", "categoria": "LÁCTEOS"}).json()

    assert data["stock_total"] == 6 and data["lotes"] == 3 and data["usuarios"] == 1
    assert [p["marca"] for p in data["productos"]] == ["C", "B", "A"]
    assert data["productos"][0]["owner"]["email"] == auth_user.email


def test_detalle_del_grupo_se_puede_acotar_a_un_usuario(admin_client, db_session, auth_user):
    otro = otro_usuario(db_session)
    create_product(db_session, auth_user, nombre="Arroz", categoria="Almacén", stock=2)
    create_product(db_session, otro, nombre="Arroz", categoria="Almacén", stock=5)

    params = {"nombre": "Arroz", "categoria": "Almacén"}
    assert admin_client.get("/admin/products/group", params=params).json()["stock_total"] == 7
    acotado = admin_client.get("/admin/products/group", params={**params, "user_id": str(otro.id)}).json()
    assert acotado["stock_total"] == 5 and acotado["usuarios"] == 1


def test_detalle_de_un_grupo_que_no_existe_da_404(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Arroz")

    resp = admin_client.get("/admin/products/group", params={"nombre": "Nada", "categoria": "Nada"})

    assert resp.status_code == 404
    assert admin_client.get("/admin/products/group", params={"nombre": "Arroz"}).status_code == 422


def test_las_rutas_de_grupos_no_se_confunden_con_el_id_de_un_producto(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Arroz")

    assert admin_client.get("/admin/products/groups", params={"q": "arroz"}).status_code == 200
