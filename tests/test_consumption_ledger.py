from app.models.app_models import ConsumptionEvent

from .conftest import create_product


def test_create_product_merge_logs_restocked_event(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Yerba", stock=5)

    resp = client.post(
        "/me/products",
        json={"nombre": "Yerba", "marca": "La Serenísima", "categoria": "Lácteos", "stock": 3},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["merged"] is True
    assert body["stock"] == 8

    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == body["id"]).all()
    assert len(events) == 1
    assert events[0].event_type == "restocked"
    assert events[0].quantity == 3


def test_update_product_stock_change_logs_events(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    resp = client.patch(f"/me/products/{product.id}", json={"stock": 6})
    assert resp.status_code == 200
    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
    assert len(events) == 1
    assert events[0].event_type == "consumed"
    assert events[0].quantity == 4

    resp = client.patch(f"/me/products/{product.id}", json={"stock": 9})
    assert resp.status_code == 200
    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
    assert len(events) == 2
    assert events[1].event_type == "restocked"
    assert events[1].quantity == 3


def test_register_salida_success(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    resp = client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 4, "event_type": "consumed"},
    )
    assert resp.status_code == 200
    assert resp.json()["stock"] == 6

    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
    assert len(events) == 1
    assert events[0].event_type == "consumed"
    assert events[0].quantity == 4


def test_register_salida_insufficient_stock(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=2)

    resp = client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 5, "event_type": "consumed"},
    )
    assert resp.status_code == 409

    db_session.refresh(product)
    assert product.stock == 2
    assert db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).count() == 0


def test_update_consumption_event_creates_reversal_and_corrected_row(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 3, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()

    resp = client.put(
        f"/me/products/{product.id}/events/{original.id}",
        json={"quantity": 5, "event_type": "consumed"},
    )
    assert resp.status_code == 200
    assert resp.json()["remaining_stock"] == 5  # 10 - 5

    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
    assert len(events) == 3
    reversal = next(e for e in events if e.reverses_event_id == original.id)
    assert reversal.event_type == "restocked"
    assert reversal.quantity == 3
    corrected = next(e for e in events if e.id not in (original.id, reversal.id))
    assert corrected.quantity == 5
    assert corrected.event_type == "consumed"


def test_cannot_correct_already_corrected_event(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 3, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    client.put(f"/me/products/{product.id}/events/{original.id}", json={"quantity": 5, "event_type": "consumed"})

    resp = client.put(f"/me/products/{product.id}/events/{original.id}", json={"quantity": 1, "event_type": "consumed"})
    assert resp.status_code == 400


def test_cannot_correct_a_reversal_row(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 3, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    client.put(f"/me/products/{product.id}/events/{original.id}", json={"quantity": 5, "event_type": "consumed"})
    reversal = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.reverses_event_id == original.id).one()

    resp = client.put(f"/me/products/{product.id}/events/{reversal.id}", json={"quantity": 1, "event_type": "consumed"})
    assert resp.status_code == 400


def test_update_consumption_event_negative_stock_guard(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=5)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 3, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()

    resp = client.put(
        f"/me/products/{product.id}/events/{original.id}",
        json={"quantity": 999, "event_type": "consumed"},
    )
    assert resp.status_code == 422

    db_session.refresh(product)
    assert product.stock == 2
    assert db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).count() == 1


def test_delete_consumption_event_undoes_it(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 4, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()

    resp = client.delete(f"/me/products/{product.id}/events/{original.id}")
    assert resp.status_code == 200
    assert resp.json()["remaining_stock"] == 10

    events = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).all()
    assert len(events) == 2
    reversal = next(e for e in events if e.reverses_event_id == original.id)
    assert reversal.event_type == "restocked"
    assert reversal.quantity == 4


def test_get_consumption_history_reversed_flag(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post(
        "/me/products/salida",
        json={"product_id": product.id, "quantity": 4, "event_type": "consumed"},
    )
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    client.delete(f"/me/products/{product.id}/events/{original.id}")

    resp = client.get(f"/me/products/{product.id}/consumption")
    assert resp.status_code == 200
    by_id = {e["id"]: e for e in resp.json()}
    assert by_id[original.id]["reversed"] is True
    reversal = next(e for e in resp.json() if e["reverses_event_id"] == original.id)
    assert reversal["reversed"] is False
