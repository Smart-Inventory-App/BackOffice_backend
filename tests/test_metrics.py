from app.models.app_models import ConsumptionEvent

from .conftest import create_product


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
