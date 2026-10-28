"""
Ramas puntuales que ningún otro archivo ejercitaba: ni son un flujo nuevo ni
merecen su propio archivo, así que se juntan acá.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.exc import SQLAlchemyError

from app.api import ocr as ocr_api
from app.core import barcode, matching, ocr_audit
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_bearer_token, get_current_admin, get_current_user
from app.main import app
from app.models.admin import AdminUser
from app.models.app_models import AuthUser
from tests.conftest import create_admin, create_product

PHOTO = ("codigo.jpg", b"\xff\xd8foto-de-prueba", "image/jpeg")


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


@pytest.fixture()
def ocr_transport(monkeypatch):
    def install(handler):
        received = []

        def record(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return handler(request)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(record)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(ocr_api.httpx, "AsyncClient", FakeClient)
        return received

    return install


# ── ocr_gateway: validación de la imagen antes de la red ──────────────────────

def test_un_archivo_vacio_se_rechaza_antes_de_la_red(admin_client, ocr_transport):
    received = ocr_transport(lambda _: httpx.Response(200, json={}))

    resp = admin_client.post(
        "/admin/ocr/scan", files={"file": ("vacia.jpg", b"", "image/jpeg")}, data={"doc_type": "barcode"}
    )

    assert resp.status_code == 400
    assert "vacío" in resp.json()["detail"]
    assert received == []


def test_un_archivo_demasiado_grande_se_rechaza_antes_de_la_red(admin_client, ocr_transport):
    received = ocr_transport(lambda _: httpx.Response(200, json={}))
    demasiado_grande = b"\xff" * (10 * 1024 * 1024 + 1)

    resp = admin_client.post(
        "/admin/ocr/scan",
        files={"file": ("grande.jpg", demasiado_grande, "image/jpeg")},
        data={"doc_type": "barcode"},
    )

    assert resp.status_code == 413
    assert received == []


def test_un_error_del_coordinador_sin_cuerpo_json_se_recorta_al_texto(admin_client, ocr_transport):
    # No todo 5xx trae un JSON prolijo: un proxy caído puede devolver HTML.
    ocr_transport(lambda _: httpx.Response(503, text="<html>Service Unavailable</html>"))

    resp = admin_client.post("/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "barcode"})

    assert resp.status_code == 503
    assert "Service Unavailable" in resp.json()["detail"]


# ── ocr_audit: ramas defensivas ───────────────────────────────────────────────

def test_ya_registrado_no_hace_fallar_a_quien_pregunta_si_la_consulta_falla():
    class DBQueRompe:
        def query(self, *a, **k):
            raise SQLAlchemyError("la tabla no está")

    assert ocr_audit._ya_registrado(DBQueRompe(), "algún-job") is False


def test_el_resumen_es_vacio_cuando_no_hay_texto_legible_ni_error():
    assert ocr_audit._resumen({}, None, "done") is None


# ── security: el camino real (sin overridear las dependencias) ───────────────

def _token(**claims) -> str:
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def test_get_current_user_real_deja_pasar_a_una_cuenta_activa(db_session):
    user = AuthUser(email="real@x.com", hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.get("/admin/auth/me", headers={"Authorization": f"Bearer {_token(email='real@x.com')}"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["email"] == "real@x.com"


def test_get_bearer_token_real_reenvia_el_token_tal_cual(db_session, ocr_transport):
    user = AuthUser(email="escaner@x.com", hashed_password="x", is_active=True)
    db_session.add(user)
    db_session.commit()
    recibidas = ocr_transport(lambda _: httpx.Response(202, json={"job_id": "j-1", "status": "queued"}))

    def override_get_db():
        yield db_session

    token = _token(email="escaner@x.com")
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/me/products/ocr/scan",
                files={"file": PHOTO},
                data={"doc_type": "product_label"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 202
    assert recibidas[0].headers["authorization"] == f"Bearer {token}"


# ── barcode.es_valido ──────────────────────────────────────────────────────────

def test_es_valido_acepta_lo_que_normaliza_y_rechaza_lo_que_no():
    assert barcode.es_valido("7794640172021") is True
    assert barcode.es_valido("7794640172020") is True  # no se exige el dígito verificador
    assert barcode.es_valido("12345") is False  # el largo no es el de un código de barras
    assert barcode.es_valido(None) is False


# ── matching: casos de borde directos ─────────────────────────────────────────

def test_normalizar_de_un_texto_vacio_es_cadena_vacia():
    assert matching.normalizar(None) == ""
    assert matching.normalizar("") == ""


def test_parecido_con_un_lado_vacio_es_cero():
    assert matching.parecido("", "Leche entera") == 0.0
    assert matching.parecido("Leche entera", "") == 0.0


def test_es_abreviatura_con_cadenas_vacias_es_falso():
    assert matching._es_abreviatura("", "leche entera") is False
    assert matching._es_abreviatura("leche", "") is False


def test_buscar_sin_texto_leido_no_devuelve_candidatos():
    assert matching.buscar(None, []) == []
    assert matching.buscar("", []) == []


# ── me.py: ramas sueltas ──────────────────────────────────────────────────────

def test_patch_de_un_producto_ajeno_o_inexistente_da_404(client):
    resp = client.patch("/me/products/999999", json={"stock": 1})

    assert resp.status_code == 404


def test_salida_de_un_producto_inexistente_da_404(client):
    resp = client.post("/me/products/salida", json={"product_id": 999999, "quantity": 1})

    assert resp.status_code == 404


def test_export_my_products_filtra_por_busqueda_y_vencido_true(client, db_session, auth_user):
    from datetime import date

    create_product(db_session, auth_user, nombre="Leche vieja", vencimiento=date(2020, 1, 1))
    create_product(db_session, auth_user, nombre="Detergente", vencimiento=date(2020, 1, 1))

    resp = client.get(
        "/me/products/export",
        params={"format": "csv", "search": "leche", "vencido": True},
    )

    text = resp.content.decode("utf-8-sig")
    assert "Leche vieja" in text
    assert "Detergente" not in text


def test_coerce_optional_int_acepta_un_entero_positivo_valido():
    from app.api.me import _coerce_optional_int

    assert _coerce_optional_int("5", "stock_minimo") == (5, None)


def test_coerce_vencimiento_acepta_un_objeto_date_directo():
    from datetime import date as date_cls

    from app.api.me import _coerce_vencimiento

    assert _coerce_vencimiento(date_cls(2026, 1, 1)) == (date_cls(2026, 1, 1), None)


def test_bulk_parse_xlsx_salta_una_fila_completamente_vacia(client):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["nombre", "categoria", "stock"])
    ws.append(["Ibuprofeno", "Farmacia", 3])
    ws.append([None, None, None])
    ws.append(["Yerba", "Almacén", 2])
    buf = io.BytesIO()
    wb.save(buf)

    resp = client.post(
        "/me/products/bulk/parse",
        files={"file": ("productos.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    filas = resp.json()["rows"]
    assert len(filas) == 2
    assert [f["data"]["nombre"] for f in filas] == ["Ibuprofeno", "Yerba"]
