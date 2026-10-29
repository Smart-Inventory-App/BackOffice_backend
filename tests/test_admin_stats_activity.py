"""
Panel de administración: tablero de estadísticas y el registro de actividad.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.argentina import ar_today
from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.app_models import AuthUser
from app.models.audit import AdminActivityLog, AdminRoleAudit, OCRScanAudit, UserStatusAudit
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


def test_activity_se_puede_filtrar_por_usuario_en_todos_los_tipos_de_evento(admin_client, db_session, auth_user):
    from app.models.app_models import AuthUser, ConsumptionEvent

    otro = AuthUser(email="otro@x.com", hashed_password="x", is_active=True)
    db_session.add(otro)
    db_session.commit()
    mio, suyo = str(auth_user.id), str(otro.id)
    producto = create_product(db_session, auth_user, nombre="Leche")
    db_session.add(UserStatusAudit(user_id=mio, changed_by="jefe@x.com", is_active=False))
    db_session.add(UserStatusAudit(user_id=suyo, changed_by="jefe@x.com", is_active=False))
    db_session.add(AdminRoleAudit(user_id=mio, changed_by="jefe@x.com", action="granted"))
    db_session.add(AdminRoleAudit(user_id=suyo, changed_by="jefe@x.com", action="granted"))
    for uid, email in ((mio, auth_user.email), (suyo, "otro@x.com")):
        db_session.add(AdminActivityLog(
            event_type="login", actor_email=email, target_user_id=uid, target_email=email, description=f"{email} inició sesión",
        ))
    db_session.add(ConsumptionEvent(product_id=producto.id, user_id=mio, quantity=1, event_type="consumed"))
    db_session.add(ConsumptionEvent(product_id=producto.id, user_id=suyo, quantity=1, event_type="consumed"))
    db_session.commit()

    todos = admin_client.get("/admin/activity/", params={"limit": 200}).json()
    solo_mio = admin_client.get("/admin/activity/", params={"user_id": mio, "limit": 200}).json()
    solo_suyo_salidas = admin_client.get("/admin/activity/", params={"user_id": suyo, "kind": "product_exit"}).json()

    assert todos["total"] > solo_mio["total"]
    assert {e["kind"] for e in solo_mio["items"]} >= {"status", "role", "login", "product_exit", "registered"}
    assert all(e["target_user_id"] == mio for e in solo_mio["items"])
    assert solo_suyo_salidas["total"] == 1 and solo_suyo_salidas["items"][0]["target_user_id"] == suyo


def test_activity_filtra_por_usuario_tambien_los_productos_y_los_eventos_del_log(admin_client, db_session, auth_user):
    uid = str(auth_user.id)
    db_session.add(AdminActivityLog(
        event_type="product_created", actor_email=auth_user.email, target_user_id=uid, target_email=auth_user.email, description="cargó",
    ))
    db_session.commit()

    assert admin_client.get("/admin/activity/", params={"user_id": uid, "kind": "product_created"}).json()["total"] == 1
    assert admin_client.get("/admin/activity/", params={"user_id": "no-existe", "kind": "product_created"}).json()["total"] == 0


def test_activity_ofrece_los_usuarios_para_filtrar_ordenados_por_mail(admin_client, db_session, auth_user):
    from app.models.app_models import AuthUser

    db_session.add(AuthUser(email="a@x.com", hashed_password="x", is_active=True))
    db_session.commit()

    usuarios = admin_client.get("/admin/activity/filters/options").json()["users"]

    assert [u["email"] for u in usuarios] == ["a@x.com", auth_user.email]
    assert usuarios[1]["user_id"] == str(auth_user.id)


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


# ── resumen de la plataforma (dashboard del administrador) ───────────────────

def _con_alta_reciente(auth_user):
    auth_user.created_at = datetime.now(timezone.utc).replace(tzinfo=None)


def test_overview_cuenta_usuarios_altas_y_adopcion(admin_client, db_session, auth_user):
    from app.models.app_models import AuthUser, ConsumptionEvent

    _con_alta_reciente(auth_user)
    viejo = AuthUser(email="viejo@x.com", hashed_password="x", is_active=False, created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30))
    db_session.add(viejo)
    producto = create_product(db_session, auth_user, nombre="Leche")
    db_session.add(ConsumptionEvent(product_id=producto.id, user_id=str(auth_user.id), quantity=2, event_type="consumed"))
    db_session.add(AdminActivityLog(
        event_type="login", actor_email="x@x.com", target_user_id=str(viejo.id), target_email="viejo@x.com", description="inició sesión",
    ))
    db_session.commit()

    data = admin_client.get("/admin/stats/overview", params={"period": 7}).json()

    users = data["users"]
    assert data["period_days"] == 7
    assert (users["total"], users["active"]) == (2, 1)
    assert users["new_in_period"] == 1
    # el que movió stock y el que inició sesión cuentan como activos en el período
    assert users["active_in_period"] == 2
    assert users["with_products"] == 1
    assert len(users["registrations_per_day"]) == 7 and sum(d["count"] for d in users["registrations_per_day"]) == 1


def test_overview_arma_la_actividad_por_dia(admin_client, db_session, auth_user):
    from app.models.app_models import ConsumptionEvent

    uid = str(auth_user.id)
    producto = create_product(db_session, auth_user, nombre="Leche", stock=50)
    for cantidad, tipo in ((3, "consumed"), (2, "sold"), (4, "expired"), (5, "restocked")):
        db_session.add(ConsumptionEvent(product_id=producto.id, user_id=uid, quantity=cantidad, event_type=tipo))
    db_session.add(AdminActivityLog(
        event_type="login", actor_email=auth_user.email, target_user_id=uid, target_email=auth_user.email, description="inició sesión",
    ))
    db_session.commit()

    data = admin_client.get("/admin/stats/overview", params={"period": 7}).json()

    actividad = data["activity_per_day"]
    assert len(actividad) == 7
    assert (sum(d["logins"] for d in actividad), sum(d["entradas"] for d in actividad), sum(d["salidas"] for d in actividad)) == (1, 1, 3)
    assert "movements" not in data


def test_overview_rankea_a_los_10_usuarios_mas_activos_con_sus_entradas_salidas_y_perfil(admin_client, db_session, auth_user):
    from app.models.app_models import AuthUser, ConsumptionEvent, UserProfile

    usuarios = [auth_user]
    for i in range(11):
        otro = AuthUser(email=f"u{i}@x.com", hashed_password="x", is_active=True)
        db_session.add(otro)
        usuarios.append(otro)
    db_session.commit()
    for i, usuario in enumerate(usuarios):
        producto = create_product(db_session, usuario, nombre=f"P{i}", stock=10 + i)
        for _ in range(i + 1):
            db_session.add(ConsumptionEvent(product_id=producto.id, user_id=str(usuario.id), quantity=1, event_type="consumed"))
    # el más activo también repuso dos veces y tiene perfil; el último está deshabilitado
    top = usuarios[-1]
    for _ in range(2):
        db_session.add(ConsumptionEvent(product_id=_id_de_su_producto(db_session, top), user_id=str(top.id), quantity=1, event_type="restocked"))
    db_session.add(UserProfile(user_id=str(top.id), username="el_top", first_name="Ana", last_name="Gómez"))
    top.is_active = False
    db_session.commit()

    activos = admin_client.get("/admin/stats/overview").json()["most_active_users"]

    assert len(activos) == 10
    assert [u["movements"] for u in activos] == [14, 11, 10, 9, 8, 7, 6, 5, 4, 3]
    primero = activos[0]
    assert (primero["email"], primero["name"], primero["username"]) == ("u10@x.com", "Ana Gómez", "el_top")
    assert (primero["entries"], primero["exits"], primero["products"], primero["units"], primero["is_active"]) == (2, 12, 1, 21, False)
    assert activos[1]["name"] is None and activos[1]["username"] is None
    assert set(primero) == {"user_id", "email", "name", "username", "movements", "entries", "exits", "products", "units", "is_active"}


def _id_de_su_producto(db_session, usuario):
    from app.models.app_models import Product

    return db_session.query(Product).filter(Product.user_id == str(usuario.id)).first().id


def test_overview_incluye_la_calidad_de_datos_y_el_rendimiento_del_ocr(admin_client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Con todo", stock=4, vencimiento=date.today() + timedelta(days=9), imagen_url="http://x/1.jpg")
    create_product(db_session, auth_user, nombre="Sin nada", stock=6)
    db_session.add(OCRScanAudit(user_email=auth_user.email, origen="app", doc_type="barcode", status="done", processing_ms=200, confianza=0.9))
    db_session.add(OCRScanAudit(user_email=auth_user.email, origen="app", doc_type="receipt", status="failed", processing_ms=1000, confianza=0.5))
    db_session.commit()

    data = admin_client.get("/admin/stats/overview").json()

    inv = data["inventory"]
    assert inv == {"products": 2, "units": 10, "with_image": 1, "without_expiry": 1}
    ocr = data["ocr"]
    assert (ocr["runs"], ocr["succeeded"]) == (2, 1)
    assert ocr["avg_ms"] == 600 and ocr["avg_confidence"] == 0.7
    assert {t["doc_type"] for t in ocr["by_type"]} == {"barcode", "receipt"}
    assert data["period_days"] == 14


def test_overview_sin_datos_devuelve_todo_en_cero(admin_client):
    data = admin_client.get("/admin/stats/overview", params={"period": 7}).json()

    assert data["users"]["total"] == 0 and data["most_active_users"] == []
    assert data["inventory"]["products"] == 0 and data["ocr"]["runs"] == 0
    assert data["ocr"]["avg_ms"] is None and data["ocr"]["by_type"] == []


def test_overview_requiere_admin():
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        assert c.get("/admin/stats/overview").status_code in (401, 403)
