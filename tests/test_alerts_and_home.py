"""
Lo que el backoffice toma de la app: hora argentina, reglas y niveles de las alertas, Centro de Alertas del usuario,
«Últimos cambios», perfil con límites y nombre de usuario único, y subida de la imagen de un producto como archivo.
"""
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.api import me_images
from app.core import alert_rules, alerts_view
from app.core.app_backend import refresh_user_alerts
from app.core.argentina import (
    ar_day,
    ar_hour_to_utc,
    ar_now,
    ar_today,
    utc_hour_to_ar,
)
from app.models.app_models import ConsumptionEvent, Product, UserProfile
from tests.conftest import create_product, create_stock_alert


def days_from_today(days: int) -> date:
    return ar_today() + timedelta(days=days)


# ── hora argentina ────────────────────────────────────────────────────────────

def test_ar_now_is_three_hours_behind_utc():
    assert ar_now().utcoffset() == timedelta(hours=-3)
    assert ar_today() == ar_now().date()


def test_the_day_of_an_instant_is_the_argentine_one():
    assert ar_day(datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)).isoformat() == "2026-10-12"
    assert ar_day(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)).isoformat() == "2026-10-13"
    assert ar_day(datetime(2026, 10, 13, 1, 30)).isoformat() == "2026-10-12"  # sin zona se asume UTC


def test_the_check_hour_moves_between_argentina_and_utc():
    assert ar_hour_to_utc(5) == 8
    assert ar_hour_to_utc(22) == 1
    assert utc_hour_to_ar(8) == 5
    assert utc_hour_to_ar(1) == 22


# ── reglas ────────────────────────────────────────────────────────────────────

def test_the_rule_of_the_product_beats_the_one_of_the_profile_and_the_default():
    assert alert_rules.resolve_limit(3, 10, 7) == (3, "product")
    assert alert_rules.resolve_limit(None, 10, 7) == (10, "general")
    assert alert_rules.resolve_limit(None, None, 7) == (7, "general")


def test_the_limits_come_from_the_profile_when_there_is_one():
    profile = UserProfile(user_id="x", stock_minimo=4, dias_alerta_vencimiento=9)
    assert alert_rules.coverage_limit(None, profile) == (4, "general")
    assert alert_rules.expiry_limit(None, profile) == (9, "general")
    assert alert_rules.expiry_limit(2, profile) == (2, "product")
    assert alert_rules.coverage_limit(None, None) == (7, "general")


@pytest.mark.parametrize(
    "days, level",
    [(-3, "critical"), (0, "critical"), (6, "critical"), (7, "warning"), (10, "warning"), (11, None), (30, None)],
)
def test_expiry_is_critical_below_the_limit_and_warning_near_it(days, level):
    assert alert_rules.expiry_level(days, 7) == level  # «menos de» 7 días; cerca hasta 4 días más


@pytest.mark.parametrize("days, level", [(0, "critical"), (6, "critical"), (7, "warning"), (10, "warning"), (11, None)])
def test_coverage_uses_the_same_criterion(days, level):
    assert alert_rules.coverage_level(days, 7) == level


def test_coverage_without_a_prediction_has_no_level():
    assert alert_rules.coverage_level(None, 7) is None


def test_the_near_margin_is_half_the_limit_with_a_minimum_of_two_days():
    assert alert_rules.near_margin(2) == 2
    assert alert_rules.near_margin(7) == 4
    assert alert_rules.near_margin(30) == 15


# ── alertas del usuario ───────────────────────────────────────────────────────

def test_the_expiry_alerts_are_computed_with_today_and_the_rule_of_the_user(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Vencido", vencimiento=days_from_today(-2))
    create_product(db_session, auth_user, nombre="Hoy", vencimiento=days_from_today(0))
    create_product(db_session, auth_user, nombre="Cerca", vencimiento=days_from_today(9))
    create_product(db_session, auth_user, nombre="Lejos", vencimiento=days_from_today(40))
    create_product(db_session, auth_user, nombre="Sin stock", stock=0, vencimiento=days_from_today(1))
    create_product(db_session, auth_user, nombre="Sin fecha")

    body = client.get("/me/alerts/").json()

    names = {a["product_name"]: a["level"] for a in body["items"]}
    assert names == {"Vencido": "critical", "Hoy": "critical", "Cerca": "warning"}
    assert body["summary"] == {"critical": 2, "warning": 1, "total": 3, "expiring": 2, "low_stock": 0}
    first = body["items"][0]
    assert first["type"] == "expiry" and first["origin"] == "rule" and first["predictive"] is False
    assert first["limit_days"] == 7 and first["rule_scope"] == "general"


def test_the_rule_of_the_product_changes_the_level(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Con regla", vencimiento=days_from_today(20), dias_alerta_vencimiento=30)

    alert = client.get("/me/alerts/").json()["items"][0]

    assert alert["level"] == "critical" and alert["limit_days"] == 30 and alert["rule_scope"] == "product"


def test_the_rule_of_the_profile_applies_to_every_product(client, db_session, auth_user):
    client.put("/me/profile", json={"dias_alerta_vencimiento": 20})
    create_product(db_session, auth_user, nombre="A los 15", vencimiento=days_from_today(15))

    alert = client.get("/me/alerts/").json()["items"][0]

    assert alert["level"] == "critical" and alert["limit_days"] == 20


def test_the_stock_alerts_come_from_the_prediction_and_get_their_level_from_the_rule(client, db_session, auth_user):
    critical = create_product(db_session, auth_user, nombre="Se acaba", stock=2)
    near = create_product(db_session, auth_user, nombre="Casi", stock=5)
    far = create_product(db_session, auth_user, nombre="Sobra", stock=50)
    create_stock_alert(db_session, auth_user, critical, days_until_stockout=3, suggested_qty=12)
    create_stock_alert(db_session, auth_user, near, days_until_stockout=9)
    create_stock_alert(db_session, auth_user, far, days_until_stockout=40)  # con la regla de hoy ya no corresponde

    body = client.get("/me/alerts/", params={"tipo": "stock"}).json()

    assert [(a["product_name"], a["level"]) for a in body["items"]] == [("Se acaba", "critical"), ("Casi", "warning")]
    first = body["items"][0]
    assert first["predictive"] is True and first["days_until_stockout"] == 3 and first["suggested_qty"] == 12
    assert body["summary"]["low_stock"] == 1


def test_a_slow_pace_alert_is_a_system_one_and_critical_inside_the_expiry_limit(client, db_session, auth_user):
    soon = create_product(db_session, auth_user, nombre="Lento", stock=30, vencimiento=days_from_today(5))
    later = create_product(db_session, auth_user, nombre="Más lento", stock=30, vencimiento=days_from_today(40))
    create_stock_alert(db_session, auth_user, soon, type="slow_pace", units_wasted=12)
    create_stock_alert(db_session, auth_user, later, type="slow_pace", units_wasted=3)

    items = {a["product_name"]: a for a in client.get("/me/alerts/", params={"tipo": "vencimiento"}).json()["items"]}

    assert items["Lento"]["origin"] == "system" and items["Lento"]["level"] == "critical"
    assert items["Lento"]["units_wasted"] == 12 and items["Lento"]["predictive"] is True
    assert items["Más lento"]["level"] == "warning"


def test_alerts_without_a_prediction_or_waste_are_ignored(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=30, vencimiento=days_from_today(40))
    create_stock_alert(db_session, auth_user, product, type="slow_pace", units_wasted=0)
    create_stock_alert(db_session, auth_user, product, type="restock")
    empty = create_product(db_session, auth_user, nombre="Vacío", stock=0)
    create_stock_alert(db_session, auth_user, empty, days_until_stockout=1)

    assert client.get("/me/alerts/").json()["items"] == []


def test_the_rule_alerts_go_first_and_the_critical_ones_before_the_warnings(client, db_session, auth_user):
    stock = create_product(db_session, auth_user, nombre="Stock", stock=2)
    create_stock_alert(db_session, auth_user, stock, days_until_stockout=2)
    lento = create_product(db_session, auth_user, nombre="Lento", stock=30, vencimiento=days_from_today(40))
    create_stock_alert(db_session, auth_user, lento, type="slow_pace", units_wasted=4)
    create_product(db_session, auth_user, nombre="Aviso", vencimiento=days_from_today(9))
    create_product(db_session, auth_user, nombre="Crítico", vencimiento=days_from_today(1))

    order = [a["product_name"] for a in client.get("/me/alerts/").json()["items"]]

    assert order == ["Crítico", "Stock", "Aviso", "Lento"]


def test_the_filters_by_level_and_type_keep_the_summary_of_the_type(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Crítico", vencimiento=days_from_today(1))
    create_product(db_session, auth_user, nombre="Aviso", vencimiento=days_from_today(9))
    stock = create_product(db_session, auth_user, nombre="Stock", stock=2)
    create_stock_alert(db_session, auth_user, stock, days_until_stockout=2)

    only_warning = client.get("/me/alerts/", params={"level": "warning"}).json()
    assert [a["product_name"] for a in only_warning["items"]] == ["Aviso"]
    assert only_warning["summary"]["critical"] == 2  # el resumen no sigue al nivel, solo al tipo

    expiry = client.get("/me/alerts/", params={"tipo": "vencimiento"}).json()
    assert expiry["summary"] == {"critical": 1, "warning": 1, "total": 2, "expiring": 1, "low_stock": 0}


def test_the_alert_summary_endpoint_counts_everything(client, db_session, auth_user):
    create_product(db_session, auth_user, vencimiento=days_from_today(1))

    assert client.get("/me/alerts/summary").json()["critical"] == 1


def test_the_alerts_of_other_users_are_not_shown(client, db_session, auth_user):
    other = Product(user_id="otro-usuario", nombre="Ajeno", categoria="X", stock=3, vencimiento=days_from_today(1))
    db_session.add(other)
    db_session.commit()

    assert client.get("/me/alerts/").json()["items"] == []


def test_levels_by_product_gives_the_worst_level_and_every_type(db_session, auth_user):
    both = create_product(db_session, auth_user, nombre="Ambas", stock=3, vencimiento=days_from_today(9))
    create_stock_alert(db_session, auth_user, both, days_until_stockout=2)
    quiet = create_product(db_session, auth_user, nombre="Tranquilo", vencimiento=days_from_today(90))

    found = alerts_view.levels_by_product(db_session, [both.id, quiet.id])

    level, types = found[both.id]
    assert level == "critical" and set(types) == {"stock_low_coverage", "expiry"}
    assert quiet.id not in found
    assert alerts_view.levels_by_product(db_session, []) == {}


# ── «Últimos cambios» ─────────────────────────────────────────────────────────

def add_event(db_session, product, quantity, event_type="consumed", when=None, note=None, **extra):
    event = ConsumptionEvent(
        product_id=product.id, user_id=product.user_id, quantity=quantity, event_type=event_type,
        consumed_at=when or datetime.now(timezone.utc), note=note, **extra,
    )
    db_session.add(event)
    db_session.commit()
    return event


def test_the_changes_show_each_movement_with_the_stock_before_and_after(client, db_session, auth_user):
    now = datetime.now(timezone.utc)
    product = create_product(db_session, auth_user, nombre="Leche", stock=7)
    product.created_at = now - timedelta(days=3)
    add_event(db_session, product, 5, "restocked", now - timedelta(days=2))
    add_event(db_session, product, 3, "consumed", now - timedelta(days=1))
    add_event(db_session, product, 2, "sold", now, note="Modificación de stock")

    changes = client.get("/me/changes").json()

    assert [(c["kind"], c["stock_before"], c["stock_after"], c["quantity"]) for c in changes] == [
        ("modificacion", 9, 7, 2),
        ("salida", 12, 9, 3),
        ("entrada", 7, 12, 5),
        ("entrada", 0, 7, 7),  # con lo que se cargó el producto
    ]
    assert changes[0]["producto"]["nombre"] == "Leche"


def test_the_changes_are_paged_and_skip_the_corrections(client, db_session, auth_user):
    now = datetime.now(timezone.utc)
    product = create_product(db_session, auth_user, stock=4)
    product.created_at = now - timedelta(days=9)
    wrong = add_event(db_session, product, 9, "consumed", now - timedelta(days=5))
    add_event(db_session, product, 9, "restocked", now - timedelta(days=5, seconds=-1), reverses_event_id=wrong.id)
    add_event(db_session, product, 1, "consumed", now - timedelta(days=1))
    add_event(db_session, product, 2, "restocked", now)

    everything = client.get("/me/changes", params={"limit": 50}).json()
    first_page = client.get("/me/changes", params={"limit": 2}).json()
    second_page = client.get("/me/changes", params={"limit": 2, "offset": 2}).json()

    assert [c["kind"] for c in everything] == ["entrada", "salida", "entrada"]
    assert [c["id"] for c in first_page + second_page] == [c["id"] for c in everything]


def test_a_product_without_creation_date_or_stock_leaves_no_initial_entry(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=0)
    product.created_at = None
    db_session.commit()

    assert client.get("/me/changes").json() == []


def test_editing_the_stock_of_a_product_leaves_a_modification_in_the_changes(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    client.patch(f"/me/products/{product.id}", json={"stock": 6})
    client.patch(f"/me/products/{product.id}", json={"stock": 9})

    kinds = [(c["kind"], c["stock_before"], c["stock_after"]) for c in client.get("/me/changes").json()]
    assert kinds[:2] == [("modificacion", 6, 9), ("modificacion", 10, 6)]


# ── perfil ────────────────────────────────────────────────────────────────────

def test_the_profile_shows_the_check_hour_in_argentina_and_saves_it_in_utc(client, db_session, auth_user):
    resp = client.put("/me/profile", json={"alert_check_hour": 22, "user_type": "comercio"})

    assert resp.json()["alert_check_hour"] == 22 and resp.json()["user_type"] == "comercio"
    saved = db_session.query(UserProfile).filter(UserProfile.user_id == str(auth_user.id)).one()
    assert saved.alert_check_hour == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"first_name": "x" * 21},
        {"last_name": "x" * 21},
        {"username": "x" * 21},
        {"stock_minimo": 1},
        {"stock_minimo": 91},
        {"dias_alerta_vencimiento": 1},
        {"dias_alerta_vencimiento": 91},
        {"alert_check_hour": 24},
        {"alert_check_hour": -1},
        {"user_type": "otro"},
    ],
)
def test_the_profile_keeps_the_limits_of_the_app(client, payload):
    assert client.put("/me/profile", json=payload).status_code == 422


def test_the_limits_of_the_profile_are_inclusive(client):
    resp = client.put("/me/profile", json={"first_name": "x" * 20, "stock_minimo": 2, "dias_alerta_vencimiento": 90, "alert_check_hour": 0})

    assert resp.status_code == 200


def test_a_username_that_belongs_to_another_account_is_rejected(client, db_session, auth_user):
    db_session.add(UserProfile(user_id="otra-cuenta", username="Ana"))
    db_session.commit()

    resp = client.put("/me/profile", json={"username": "ana"})

    assert resp.status_code == 409
    assert client.get("/me/profile/username-available", params={"username": "ANA"}).json() == {"available": False}
    assert client.get("/me/profile/username-available", params={"username": "ana2"}).json() == {"available": True}
    assert client.get("/me/profile/username-available", params={"username": "   "}).json() == {"available": True}


def test_the_own_username_is_available_and_can_be_saved_again(client):
    assert client.put("/me/profile", json={"username": "gian"}).status_code == 200

    assert client.get("/me/profile/username-available", params={"username": "Gian"}).json() == {"available": True}
    assert client.put("/me/profile", json={"username": "GIAN", "first_name": "Gian"}).status_code == 200


def test_a_username_repeated_from_before_does_not_block_saving_the_rest(client, db_session, auth_user):
    db_session.add(UserProfile(user_id="otra-cuenta", username="repe"))
    db_session.add(UserProfile(user_id=str(auth_user.id), username="repe"))
    db_session.commit()

    assert client.put("/me/profile", json={"username": "repe", "first_name": "Gian"}).status_code == 200


def test_clearing_the_username_is_allowed(client):
    client.put("/me/profile", json={"username": "gian"})

    resp = client.put("/me/profile", json={"username": "  "})

    assert resp.status_code == 200 and resp.json()["username"] is None


def test_changing_a_rule_asks_the_app_backend_to_review_the_alerts(client, auth_user, no_app_backend):
    client.put("/me/profile", json={"first_name": "Gian"})
    assert no_app_backend == []

    client.put("/me/profile", json={"stock_minimo": 12})
    client.put("/me/profile", json={"stock_minimo": 12})  # el mismo valor no cambia nada
    client.put("/me/profile", json={"dias_alerta_vencimiento": 15})

    assert no_app_backend == [str(auth_user.id), str(auth_user.id)]


def test_loading_or_editing_products_asks_the_app_backend_to_review_the_alerts(client, db_session, auth_user, no_app_backend):
    client.post("/me/products", json={"nombre": "Leche", "categoria": "Lácteos", "stock": 4})
    product = db_session.query(Product).one()
    client.patch(f"/me/products/{product.id}", json={"descripcion": "sin tocar el stock"})
    client.patch(f"/me/products/{product.id}", json={"stock_minimo": 5})
    client.post("/me/products/salida", json={"product_id": product.id, "quantity": 1, "event_type": "consumed"})

    assert len(no_app_backend) == 3


def test_the_review_of_the_alerts_does_not_notify_and_never_breaks_the_save(monkeypatch):
    sent = []

    def fake_post(url, params=None, headers=None, timeout=None):
        sent.append((url, params, headers))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.core.app_backend.httpx.post", fake_post)
    assert refresh_user_alerts("abc") is True
    assert sent[0][0].endswith("/internal/alerts/run/abc") and sent[0][1] == {"notify": "false"}
    assert "X-Internal-Token" in sent[0][2]

    def failing_post(*args, **kwargs):
        raise httpx.ConnectError("sin conexión")

    monkeypatch.setattr("app.core.app_backend.httpx.post", failing_post)
    assert refresh_user_alerts("abc") is False


# ── stats ─────────────────────────────────────────────────────────────────────

def test_the_stats_follow_the_same_alerts_as_the_center(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Vence", stock=4, vencimiento=days_from_today(1))
    create_product(db_session, auth_user, nombre="Aviso", stock=6, vencimiento=days_from_today(9))

    stats = client.get("/me/stats").json()
    summary = client.get("/me/alerts/summary").json()

    assert stats["total_units"] == 10
    assert (stats["critical_alerts"], stats["warning_alerts"]) == (summary["critical"], summary["warning"]) == (1, 1)
    assert stats["expiring_soon_products"] == summary["expiring"] == 1


# ── imagen del producto como archivo ──────────────────────────────────────────

@pytest.fixture()
def storage(monkeypatch):
    received = []

    def install(handler):
        def record(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return handler(request)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(record)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(me_images.httpx, "AsyncClient", FakeClient)
        return received

    return install


def image_file(content=b"\x89PNG-bytes", name="foto.png", kind="image/png"):
    return {"file": (name, content, kind)}


def test_the_image_travels_as_a_file_to_the_storage_service_and_the_url_comes_back(client, storage):
    received = storage(lambda request: httpx.Response(200, json={"url": "https://img.example/p.png"}))

    resp = client.post("/me/products/image", files=image_file(), headers={"Authorization": "Bearer token-del-usuario"})

    assert resp.status_code == 200 and resp.json() == {"url": "https://img.example/p.png"}
    sent = received[0]
    assert str(sent.url).endswith("/storage/upload")
    assert sent.headers["authorization"] == "Bearer token-del-usuario"
    assert b'name="folder"' in sent.content and b"tpp/products" in sent.content and b"PNG-bytes" in sent.content


@pytest.mark.parametrize("kind", ["application/pdf", "text/plain", "image/gif"])
def test_only_jpg_png_and_webp_are_accepted(client, storage, kind):
    received = storage(lambda request: httpx.Response(200, json={"url": "x"}))

    resp = client.post("/me/products/image", files=image_file(kind=kind))

    assert resp.status_code == 400 and "JPG, PNG o WebP" in resp.json()["detail"]
    assert received == []


def test_an_empty_or_huge_image_is_rejected_before_uploading_it(client, storage, monkeypatch):
    received = storage(lambda request: httpx.Response(200, json={"url": "x"}))

    assert client.post("/me/products/image", files=image_file(content=b"")).status_code == 400
    monkeypatch.setattr(me_images.settings, "MAX_IMAGE_MB", 0)
    big = client.post("/me/products/image", files=image_file(content=b"123"))

    assert big.status_code == 413 and "pesa más de" in big.json()["detail"]
    assert received == []


def test_an_error_of_the_storage_service_is_explained(client, storage):
    storage(lambda request: httpx.Response(400, json={"detail": "Formato no soportado"}))

    resp = client.post("/me/products/image", files=image_file())

    assert resp.status_code == 400 and resp.json()["detail"] == "Formato no soportado"


def test_a_storage_service_that_is_down_is_a_bad_gateway(client, storage):
    def down(request):
        raise httpx.ConnectError("sin conexión", request=request)

    storage(down)

    resp = client.post("/me/products/image", files=image_file())

    assert resp.status_code == 502 and "No se pudo subir la imagen" in resp.json()["detail"]
