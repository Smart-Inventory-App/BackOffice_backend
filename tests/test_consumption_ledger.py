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


# ── historial del producto: notas con fecha, dirección fija y «Últimos cambios» ──

def _stamp(moment):
    from datetime import timedelta, timezone

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")


def test_las_notas_de_una_correccion_dicen_la_fecha_del_registro_y_no_su_numero(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "consumed"})
    original = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    stamp = _stamp(original.consumed_at)

    client.put(f"/me/products/{product.id}/events/{original.id}", json={"quantity": 5, "event_type": "sold"})

    history = client.get(f"/me/products/{product.id}/consumption").json()
    notes = [e["note"] for e in history if e["note"]]
    assert notes == [f"Corrige el registro del {stamp}", f"Corrige el registro del {stamp}"]
    assert all("#" not in n and "antes" not in n for n in notes)


def test_una_nota_escrita_por_la_persona_se_conserva_y_deshacer_dice_deshace(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "consumed"})
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 2, "event_type": "consumed"})
    first, second = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).order_by(ConsumptionEvent.id).all()
    client.put(f"/me/products/{product.id}/events/{first.id}", json={"quantity": 4, "event_type": "consumed", "note": "Me equivoqué"})
    client.delete(f"/me/products/{product.id}/events/{second.id}")

    notes = [e["note"] for e in client.get(f"/me/products/{product.id}/consumption").json() if e["note"]]

    assert "Me equivoqué" in notes
    assert f"Deshace el registro del {_stamp(second.consumed_at)}" in notes


def test_las_notas_viejas_con_numero_se_muestran_con_la_fecha(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    old = ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=3, event_type="consumed")
    db_session.add(old)
    db_session.commit()
    db_session.refresh(old)
    db_session.add_all([
        ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=3, event_type="restocked",
                         note=f"Corrección: revierte el registro #{old.id}", reverses_event_id=old.id),
        ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=5, event_type="consumed",
                         note=f"Corrige el registro #{old.id} (antes: 3 consumed)"),
        ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=1, event_type="consumed",
                         note=f"Deshecho: revierte el registro #{old.id}"),
        ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=1, event_type="consumed",
                         note="Corrige el registro #999999 (antes: 3 consumed)"),
    ])
    db_session.commit()

    notes = [e["note"] for e in client.get(f"/me/products/{product.id}/consumption").json() if e["note"]]

    stamp = _stamp(old.consumed_at)
    assert notes == [f"Corrige el registro del {stamp}", f"Corrige el registro del {stamp}", f"Deshace el registro del {stamp}", "Corrige un registro anterior"]


def test_una_entrada_no_pasa_a_salida_ni_una_salida_a_entrada(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    client.patch(f"/me/products/{product.id}", json={"stock": 15})  # entrada de 5
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 3, "event_type": "consumed"})
    entrada, salida = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).order_by(ConsumptionEvent.id).all()
    base = f"/me/products/{product.id}/events"

    assert client.put(f"{base}/{entrada.id}", json={"quantity": 2, "event_type": "sold"}).status_code == 400
    assert client.put(f"{base}/{salida.id}", json={"quantity": 2, "event_type": "restocked"}).status_code == 400
    db_session.refresh(product)
    assert product.stock == 12
    # Una salida sí puede cambiar de motivo y una entrada puede cambiar de unidades
    assert client.put(f"{base}/{salida.id}", json={"quantity": 2, "event_type": "expired"}).status_code == 200
    assert client.put(f"{base}/{entrada.id}", json={"quantity": 4, "event_type": "restocked"}).status_code == 200


def test_editar_una_entrada_deja_una_sola_modificacion_en_los_ultimos_cambios(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=20)
    product.created_at = None
    db_session.commit()
    client.patch(f"/me/products/{product.id}", json={"stock": 20})  # sin cambios: no hay movimiento
    db_session.add(ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=15, event_type="restocked"))
    product.stock = 35
    db_session.commit()
    entry = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()

    client.put(f"/me/products/{product.id}/events/{entry.id}", json={"quantity": 13, "event_type": "restocked"})

    changes = [(c["kind"], c["stock_before"], c["stock_after"], c["quantity"]) for c in client.get("/me/changes").json()]
    assert changes == [("modificacion", 35, 33, 2), ("entrada", 20, 35, 15)]


def test_deshacer_un_registro_deja_una_modificacion_con_el_stock_de_antes_y_de_despues(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    product.created_at = None
    db_session.commit()
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 4, "event_type": "consumed"})
    salida = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()

    client.delete(f"/me/products/{product.id}/events/{salida.id}")

    changes = [(c["kind"], c["stock_before"], c["stock_after"], c["quantity"]) for c in client.get("/me/changes").json()]
    assert changes == [("modificacion", 6, 10, 4), ("salida", 10, 6, 4)]


def test_dos_ediciones_dejan_dos_modificaciones_y_el_orden_es_el_del_tiempo(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)
    product.created_at = None
    db_session.commit()
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 4, "event_type": "consumed"})
    salida = db_session.query(ConsumptionEvent).filter(ConsumptionEvent.product_id == product.id).one()
    client.put(f"/me/products/{product.id}/events/{salida.id}", json={"quantity": 3, "event_type": "sold"})
    corrected = db_session.query(ConsumptionEvent).filter(
        ConsumptionEvent.product_id == product.id, ConsumptionEvent.reverses_event_id.is_(None), ConsumptionEvent.id != salida.id
    ).one()
    client.put(f"/me/products/{product.id}/events/{corrected.id}", json={"quantity": 1, "event_type": "sold"})

    changes = [(c["kind"], c["stock_before"], c["stock_after"]) for c in client.get("/me/changes").json()]

    assert changes == [("modificacion", 7, 9), ("modificacion", 6, 7), ("salida", 10, 6)]
