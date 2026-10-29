"""Tendencias de toda la plataforma, ranking sin repetir productos y el detalle de un usuario con sus alertas y consumo."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core import alerts_view
from app.core.argentina import ar_today
from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import ConsumptionEvent
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


def add_event(db_session, product, quantity, event_type="consumed", days_ago=0):
    db_session.add(ConsumptionEvent(
        product_id=product.id, user_id=product.user_id, quantity=quantity, event_type=event_type,
        consumed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    ))
    db_session.commit()


def test_the_ranking_counts_the_lots_of_a_product_once_and_has_at_most_five(client, db_session, auth_user):
    for qty in (4, 2, 1):
        add_event(db_session, create_product(db_session, auth_user, nombre="Leche Entera"), qty)
    add_event(db_session, create_product(db_session, auth_user, nombre="leche  entera"), 1)
    for i in range(7):
        add_event(db_session, create_product(db_session, auth_user, nombre=f"P{i}"), 3)

    body = client.get("/me/metrics", params={"period": 7}).json()

    assert "productos_menos_usados" not in body
    names = [p["nombre"] for p in body["top_productos"]]
    assert len(names) == 5 and len(set(names)) == 5
    assert body["top_productos"][0] == {"product_id": body["top_productos"][0]["product_id"], "nombre": "Leche Entera", "cantidad": 8, "owner_email": None}


def test_the_ranking_keeps_the_lot_that_was_consumed_the_most_and_orders_ties_by_name(client, db_session, auth_user):
    small = create_product(db_session, auth_user, nombre="Yogur")
    big = create_product(db_session, auth_user, nombre="yogur")
    add_event(db_session, small, 1)
    add_event(db_session, big, 5)
    add_event(db_session, create_product(db_session, auth_user, nombre="Zeta"), 3)
    add_event(db_session, create_product(db_session, auth_user, nombre="Alfa"), 3)

    top = client.get("/me/metrics").json()["top_productos"]

    assert [(p["nombre"], p["cantidad"]) for p in top] == [("yogur", 6), ("Alfa", 3), ("Zeta", 3)]
    assert top[0]["product_id"] == big.id


def test_the_platform_metrics_add_up_every_user_and_are_only_for_admins(admin_client, db_session, auth_user):
    add_event(db_session, create_product(db_session, auth_user, nombre="A"), 4)
    add_event(db_session, create_product(db_session, auth_user, nombre="A"), 2, "restocked")
    other = create_product(db_session, auth_user, nombre="B", user_id="otro-usuario")
    add_event(db_session, other, 3)
    add_event(db_session, other, 9, days_ago=60)  # fuera del período

    body = admin_client.get("/admin/metrics", params={"period": 7}).json()

    assert (body["total_consumido"], body["total_entradas"]) == (7, 2)
    assert body["period_days"] == 7 and len(body["consumo_por_dia"]) == 7
    assert body["consumo_por_dia"][-1] == {"day": ar_today().isoformat(), "quantity": 7}
    assert body["top_productos"][0]["nombre"] == "A"


def test_the_platform_ranking_has_ten_products_with_the_email_of_their_owner(admin_client, db_session, auth_user):
    for i in range(12):
        add_event(db_session, create_product(db_session, auth_user, nombre=f"P{i:02d}"), 20 - i)
    # el mismo nombre en dos usuarios no se junta; el que no tiene cuenta queda sin mail
    add_event(db_session, create_product(db_session, auth_user, nombre="Repetido"), 30)
    add_event(db_session, create_product(db_session, auth_user, nombre="Repetido", user_id="otro-usuario"), 25)

    top = admin_client.get("/admin/metrics", params={"period": 7}).json()["top_productos"]

    assert len(top) == 10
    assert [(p["nombre"], p["cantidad"], p["owner_email"]) for p in top[:3]] == [
        ("Repetido", 30, auth_user.email),
        ("Repetido", 25, None),
        ("P00", 20, auth_user.email),
    ]


def test_the_platform_metrics_reject_a_user_who_is_not_an_admin(client):
    app.dependency_overrides.pop(get_current_admin, None)
    assert client.get("/admin/metrics").status_code in (401, 403)


def test_the_detail_of_a_user_shows_units_alerts_and_the_last_30_days(admin_client, db_session, auth_user):
    soon = create_product(db_session, auth_user, nombre="Vence", stock=3, vencimiento=ar_today() + timedelta(days=2))
    low = create_product(db_session, auth_user, nombre="Se acaba", stock=2, codigo_barras="7790895000997")
    create_stock_alert(db_session, auth_user, low, days_until_stockout=2)
    add_event(db_session, soon, 5)
    add_event(db_session, soon, 8, "restocked")
    add_event(db_session, soon, 9, days_ago=45)

    body = admin_client.get(f"/admin/users/{auth_user.id}").json()

    assert body["total_units"] == 5
    assert (body["critical_alerts"], body["warning_alerts"]) == (2, 0)
    assert (body["consumed_last_30d"], body["restocked_last_30d"]) == (5, 8)
    assert {a["type"] for a in body["alerts"]} == {"expiry", "stock_low_coverage"}
    by_name = {p["nombre"]: p for p in body["products"]}
    assert by_name["Vence"]["alert_level"] == "critical" and by_name["Vence"]["alert_types"] == ["expiry"]
    assert by_name["Se acaba"]["codigo_barras"] == "7790895000997"


def test_a_critical_alert_wins_over_a_warning_of_the_same_product(db_session, auth_user):
    product = create_product(db_session, auth_user, stock=4, vencimiento=ar_today() + timedelta(days=9))  # advertencia
    create_stock_alert(db_session, auth_user, product, days_until_stockout=1)  # crítica

    level, types = alerts_view.levels_by_product(db_session, [product.id])[product.id]

    assert level == "critical" and set(types) == {"expiry", "stock_low_coverage"}


def test_a_text_read_from_a_label_is_matched_against_name_brand_and_description(client, db_session, auth_user):
    yogur = create_product(db_session, auth_user, nombre="Yogur Frutilla", marca="La Serenísima", descripcion="Bebible 1 litro")
    create_product(db_session, auth_user, nombre="Detergente", marca="Ala", descripcion="Limón")

    by_name = client.get("/me/products/match", params={"q": "YOGUR FRUTILLA entero 900g"}).json()
    by_brand_and_text = client.get("/me/products/match", params={"q": "La Serenísima yogur"}).json()
    by_description = client.get("/me/products/match", params={"q": "bebible litro"}).json()

    assert [p["id"] for p in by_name] == [yogur.id]
    assert by_brand_and_text[0]["id"] == yogur.id
    assert by_description[0]["id"] == yogur.id
    assert client.get("/me/products/match", params={"q": "zzzz"}).json() == []


def test_the_matching_of_a_text_ignores_a_blank_text_and_the_products_of_other_users(db_session, auth_user):
    from app.core import matching

    assert matching.buscar_texto("   ", []) == []
    assert matching.buscar_texto(None, []) == []


def test_the_user_sees_the_total_stock_of_the_same_product_among_their_own_lots(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", marca="A", stock=3, vencimiento=ar_today() + timedelta(days=5))
    create_product(db_session, auth_user, nombre="leche ", marca="B", stock=4)
    create_product(db_session, auth_user, nombre="Leche", stock=99, user_id="otro-usuario")
    create_product(db_session, auth_user, nombre="Pan", stock=1)

    found = client.get("/me/products/groups", params={"q": "lec"}).json()
    group = client.get("/me/products/group", params={"nombre": "Leche", "categoria": "Lácteos"}).json()

    assert [(g["nombre"], g["stock_total"], g["lotes"], g["usuarios"]) for g in found] == [("Leche", 7, 2, 1)]
    assert group["stock_total"] == 7 and len(group["productos"]) == 2
    assert group["productos"][0]["alert_level"] == "critical"  # el que vence primero va primero
    assert group["productos"][0]["owner"]["email"] == auth_user.email


def test_a_group_that_does_not_exist_is_a_404_and_a_blank_search_finds_nothing(client):
    assert client.get("/me/products/group", params={"nombre": "Nada", "categoria": "X"}).status_code == 404
    assert client.get("/me/products/groups", params={"q": "  "}).json() == []


def test_the_stats_bring_what_the_dashboard_charts_need(client, db_session, auth_user):
    today = ar_today()
    create_product(db_session, auth_user, nombre="Vencida", categoria="Lácteos", stock=2, vencimiento=today - timedelta(days=1), ubicacion="Heladera")
    create_product(db_session, auth_user, nombre="Esta semana", categoria="Lácteos", stock=3, vencimiento=today + timedelta(days=3), ubicacion="Heladera")
    create_product(db_session, auth_user, nombre="Este mes", categoria="Almacén", stock=4, vencimiento=today + timedelta(days=20), ubicacion=None)
    create_product(db_session, auth_user, nombre="Trimestre", categoria="Almacén", stock=5, vencimiento=today + timedelta(days=60))
    create_product(db_session, auth_user, nombre="Lejos", categoria="Almacén", stock=6, vencimiento=today + timedelta(days=400))
    create_product(db_session, auth_user, nombre="No vence", categoria="Limpieza", stock=7, vencimiento=None)
    create_product(db_session, auth_user, nombre="Sin stock", categoria="Limpieza", stock=0, vencimiento=today + timedelta(days=2))

    stats = client.get("/me/stats").json()

    assert {b["label"]: b["count"] for b in stats["expiry_buckets"]} == {
        "Vencidos": 1, "0 a 7 días": 1, "8 a 30 días": 1, "31 a 90 días": 1, "Más de 90 días": 1, "Sin vencimiento": 1,
    }
    assert [(c["label"], c["count"]) for c in stats["stock_by_category"]] == [("Almacén", 15), ("Limpieza", 7), ("Lácteos", 5)]
    locations = {c["label"]: c["count"] for c in stats["products_by_location"]}
    assert locations == {"Heladera": 2}  # lo que no tiene ubicación no es una ubicación


# ── ingresos por día: reposiciones + el stock con el que se cargan los productos nuevos ──

def test_the_incomes_count_restocks_and_the_initial_stock_of_new_products_by_argentine_day(client, db_session, auth_user):
    today = ar_today()
    existing = create_product(db_session, auth_user, nombre="Leche", stock=12)   # 5 con los que se cargó + 7 repuestos
    add_event(db_session, existing, 7, "restocked", days_ago=0)
    create_product(db_session, auth_user, nombre="Pan", stock=4)                  # se cargó hoy con 4
    add_event(db_session, existing, 3, "consumed", days_ago=0)                    # una salida no es un ingreso

    body = client.get("/me/metrics", params={"period": 7}).json()

    assert body["total_ingresos"] == 7 + 4 + 8 and len(body["ingresos_por_dia"]) == 7
    assert body["ingresos_por_dia"][-1] == {"day": today.isoformat(), "quantity": 7 + 4 + 8}
    assert body["total_entradas"] == 7  # «repuesto» sigue contando solo las reposiciones


def test_the_incomes_leave_out_corrections_and_old_products(client, db_session, auth_user):
    from datetime import timezone

    product = create_product(db_session, auth_user, nombre="Viejo", stock=9)
    product.created_at = datetime.now(timezone.utc) - timedelta(days=40)
    db_session.commit()
    wrong = ConsumptionEvent(product_id=product.id, user_id=product.user_id, quantity=9, event_type="restocked", consumed_at=datetime.now(timezone.utc))
    db_session.add(wrong)
    db_session.commit()
    db_session.add(ConsumptionEvent(
        product_id=product.id, user_id=product.user_id, quantity=9, event_type="consumed",
        consumed_at=datetime.now(timezone.utc), reverses_event_id=wrong.id,
    ))
    db_session.commit()

    body = client.get("/me/metrics", params={"period": 7}).json()

    assert body["total_ingresos"] == 0
    assert all(day["quantity"] == 0 for day in body["ingresos_por_dia"])


def test_a_product_that_is_gone_from_stock_still_counts_what_came_in(client, db_session, auth_user):
    product = create_product(db_session, auth_user, nombre="Se fue", stock=0)
    add_event(db_session, product, 5, "consumed", days_ago=0)  # entró con 5 y salieron los 5

    assert client.get("/me/metrics", params={"period": 7}).json()["total_ingresos"] == 5


def test_the_platform_incomes_add_up_every_user(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="A", stock=3)
    create_product(db_session, auth_user, nombre="B", stock=4, user_id="otro-usuario")

    body = admin_client.get("/admin/metrics", params={"period": 7}).json()

    assert body["total_ingresos"] == 7
