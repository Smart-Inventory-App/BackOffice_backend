from app.models.app_models import ConsumptionEvent

from .conftest import create_product


def test_metrics_no_cuenta_las_entradas_en_el_consumo_diario_ni_por_producto(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=5)
    client.patch(f"/me/products/{product.id}", json={"stock": 15})  # restocked +10
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 3, "event_type": "consumed"},
    )

    data = client.get("/me/metrics", params={"period": 30}).json()

    assert data["total_entradas"] == 10
    assert data["total_consumido"] == 3
    assert sum(d["quantity"] for d in data["consumo_por_dia"]) == 3
    assert data["top_productos"][0]["cantidad"] == 3


def test_metrics_excludes_corrections_from_totals(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 4, "event_type": "consumed"},
    )

    before = client.get("/me/metrics", params={"period": 30}).json()
    assert before["total_consumido"] == 4

    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    client.put(
        f"/me/products/{product.id}/events/{original.id}",
        json={"quantity": 6, "event_type": "consumed"},
    )

    after = client.get("/me/metrics", params={"period": 30}).json()
    assert after["total_consumido"] == 6


def test_metrics_counts_sales_and_rentals_as_consumption(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=20)
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "sold"})
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 2, "event_type": "rented"})
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 4, "event_type": "expired"})

    data = client.get("/me/metrics", params={"period": 30}).json()

    assert data["total_consumido"] == 5
    assert sum(d["quantity"] for d in data["consumo_por_dia"]) == 5
    assert data["top_productos"][0]["cantidad"] == 5


def test_a_sale_or_a_rental_lowers_the_stock_and_keeps_its_type(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    resp = client.post("/me/products/salida", json={"product_id": product.id, "quantity": 4, "event_type": "sold"})

    assert resp.status_code == 200
    db_session.refresh(product)
    assert product.stock == 6
    event = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    assert event.event_type == "sold"


def test_an_unknown_kind_of_exit_is_rejected_by_the_backoffice(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    resp = client.post("/me/products/salida", json={"product_id": product.id, "quantity": 1, "event_type": "stolen"})

    assert resp.status_code == 422

def test_the_day_of_an_event_is_the_argentine_one():
    from datetime import datetime, timedelta, timezone

    from app.core.argentina import ar_day as _ar_day

    # 02:30 UTC del 13 son las 23:30 del 12 en Argentina; sin zona horaria se asume UTC
    assert _ar_day(datetime(2026, 10, 13, 2, 30)).isoformat() == "2026-10-12"
    assert _ar_day(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)).isoformat() == "2026-10-13"
    assert _ar_day(datetime(2026, 10, 13, 1, 0, tzinfo=timezone(timedelta(hours=2)))).isoformat() == "2026-10-12"

