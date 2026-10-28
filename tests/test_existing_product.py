"""«Producto existente» del flujo de producto: sumar stock a un producto ya cargado y buscarlo por código de barras."""
from datetime import date

from app.models.app_models import ConsumptionEvent, Product

from .conftest import create_product


def test_restock_suma_stock_y_deja_el_movimiento(client, db_session, auth_user):
    product = create_product(db_session, auth_user, nombre="Leche", stock=5)

    response = client.post(f"/me/products/{product.id}/restock", json={"quantity": 10})

    assert response.status_code == 201
    assert response.json()["stock"] == 15
    db_session.refresh(product)
    assert product.stock == 15
    event = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    assert (event.event_type, event.quantity, event.actor_email) == ("restocked", 10, auth_user.email)


def test_restock_pide_una_cantidad_valida_y_un_producto_propio(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=5)
    ajeno = create_product(db_session, auth_user, nombre="Ajeno", stock=1, user_id="otro-usuario")

    assert client.post(f"/me/products/{product.id}/restock", json={"quantity": 0}).status_code == 422
    assert client.post(f"/me/products/{product.id}/restock", json={"quantity": 1_000_000}).status_code == 422
    assert client.post(f"/me/products/{ajeno.id}/restock", json={"quantity": 3}).status_code == 404
    assert client.post("/me/products/999999/restock", json={"quantity": 3}).status_code == 404
    db_session.refresh(product)
    assert product.stock == 5


def test_los_productos_con_un_codigo_vienen_todos_con_el_que_vence_antes_primero(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche tarde", codigo_barras="7791234567890", vencimiento=date(2030, 1, 1))
    create_product(db_session, auth_user, nombre="Leche pronto", codigo_barras="7791234567890", vencimiento=date(2029, 1, 1))
    create_product(db_session, auth_user, nombre="Leche sin fecha", codigo_barras="7791234567890")
    create_product(db_session, auth_user, nombre="Otro", codigo_barras="7790000000001")
    create_product(db_session, auth_user, nombre="Ajeno", codigo_barras="7791234567890", user_id="otro-usuario")

    found = client.get("/me/products/by-barcode/7791234567890/all").json()

    assert [p["nombre"] for p in found] == ["Leche pronto", "Leche tarde", "Leche sin fecha"]
    assert found[0]["codigo_barras"] == "7791234567890"
    assert "imagen_url" in found[0]


def test_un_codigo_sin_productos_o_vacio_no_devuelve_nada(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", codigo_barras="7791234567890")

    assert client.get("/me/products/by-barcode/7799999999999/all").json() == []
    assert client.get("/me/products/by-barcode/---/all").json() == []
