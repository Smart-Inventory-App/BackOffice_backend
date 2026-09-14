import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.exc import SQLAlchemyError

from app.api import ocr as ocr_api
from app.core import ocr_audit
from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_admin
from app.main import app
from app.models.audit import OCRScanAudit

PHOTO = ("codigo.jpg", b"\xff\xd8foto-de-prueba", "image/jpeg")

READING = {
    "doc_type": "barcode",
    "result": {
        "fields": {
            "codigo": {"value": "7794640172021", "confidence": 1.0},
            "simbologia": {"value": "EAN-13", "confidence": 1.0},
            "pais_registro": {"value": "Argentina", "confidence": 1.0},
        },
        "items": [],
        "document_confidence": 1.0,
        "needs_review": False,
        "engine": "zbar",
        "texto_legible": "7794640172021 (EAN-13)",
        "attempts": 1,
        "processing_ms": 129,
        "warnings": [],
    },
}


class FakeAdmin:
    email = "admin@smartinventory.com"
    is_active = True


@pytest.fixture()
def admin_client(db_session):
    """Cliente autenticado como admin, sobre la base que los tests revierten.

    El estado y el escaneo no necesitan base, pero el escaneo deja su renglón de
    auditoría: sin esta sesión, cada corrida ensuciaría la base de verdad.
    """
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_admin] = lambda: FakeAdmin()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def ocr_transport(monkeypatch):
    """Instala un coordinador de mentira y devuelve las peticiones que recibió."""
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


def test_status_shows_the_checks_and_the_engines(admin_client, ocr_transport):
    def respond(request):
        if request.url.path == "/ocr/readyz":
            return httpx.Response(200, json={
                "status": "ok",
                "checks": {"redis": True, "engine": True, "barcode": True},
                "engine_name": "hybrid",
            })
        return httpx.Response(200, json=[
            {"name": "tesseract", "url": "http://tpp_ocr_tesseract:8005",
             "available": True, "version": "5.3.0"},
            {"name": "paddleocr", "url": "http://tpp_ocr_paddleocr:8007",
             "available": True, "version": "3.2.0"},
        ])

    ocr_transport(respond)

    data = admin_client.get("/admin/ocr/status").json()

    assert data["available"] is True
    assert data["checks"]["engine_name"] == "hybrid"
    assert [e["name"] for e in data["engines"]] == ["tesseract", "paddleocr"]


def test_status_does_not_fail_when_the_module_is_down(admin_client, ocr_transport):
    # Que el módulo esté caído es lo que el panel tiene que mostrar; devolver un
    # 502 lo dejaría sin poder decir qué pasa.
    def down(request):
        raise httpx.ConnectError("connection refused")

    ocr_transport(down)

    resp = admin_client.get("/admin/ocr/status")

    assert resp.status_code == 200
    assert resp.json()["available"] is False
    assert resp.json()["engines"] == []


def test_a_degraded_readiness_is_shown_with_its_diagnosis(admin_client, ocr_transport):
    def respond(request):
        if request.url.path == "/ocr/readyz":
            return httpx.Response(503, json={"detail": {
                "status": "error",
                "checks": {"redis": False, "engine": True, "barcode": True},
                "engine_name": "hybrid",
            }})
        return httpx.Response(200, json=[])

    ocr_transport(respond)

    data = admin_client.get("/admin/ocr/status").json()

    assert data["available"] is False
    assert data["checks"]["checks"]["redis"] is False


def test_scan_returns_the_whole_reading(admin_client, ocr_transport):
    received = ocr_transport(lambda _: httpx.Response(200, json=READING))

    data = admin_client.post(
        "/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "barcode"}
    ).json()

    assert data["result"]["fields"]["codigo"]["value"] == "7794640172021"
    assert data["result"]["engine"] == "zbar"
    assert data["result"]["texto_legible"]
    assert received[0].url.path == "/ocr/sync/upload"


def test_scan_is_synchronous_and_skips_the_storage(admin_client, ocr_transport):
    # `/ocr/sync/upload` procesa el binario en memoria. El escaneo de la app sí
    # pasa por la carpeta temporal, porque es asincrónico y la foto tiene que
    # sobrevivir hasta que el worker la tome.
    received = ocr_transport(lambda _: httpx.Response(200, json=READING))

    admin_client.post("/admin/ocr/scan", files={"file": PHOTO})

    assert len(received) == 1
    assert received[0].url.path == "/ocr/sync/upload"
    assert b"foto-de-prueba" in received[0].content


def test_an_engine_can_be_forced_to_compare_them(admin_client, ocr_transport):
    received = ocr_transport(lambda _: httpx.Response(200, json=READING))

    admin_client.post(
        "/admin/ocr/scan",
        files={"file": PHOTO},
        data={"doc_type": "product_label", "engine": "paddleocr"},
    )

    assert b"paddleocr" in received[0].content


def test_the_backoffice_signs_its_own_service_token(admin_client, ocr_transport):
    # El token del admin no se reenvía: se firma uno propio con el secreto
    # compartido, que es el mismo que valida el coordinador.
    received = ocr_transport(lambda _: httpx.Response(200, json=READING))

    admin_client.post("/admin/ocr/scan", files={"file": PHOTO})

    authorization = received[0].headers["authorization"]
    assert authorization.startswith("Bearer ")

    payload = jwt.decode(
        authorization.removeprefix("Bearer "),
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
    assert payload["sub"] == "backoffice"


def test_an_invalid_doc_type_is_rejected_before_the_network(admin_client, ocr_transport):
    received = ocr_transport(lambda _: httpx.Response(200, json=READING))

    resp = admin_client.post(
        "/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "recibo"}
    )

    assert resp.status_code == 400
    assert received == []


def test_an_unsupported_format_is_rejected(admin_client, ocr_transport):
    ocr_transport(lambda _: httpx.Response(200, json=READING))

    resp = admin_client.post(
        "/admin/ocr/scan", files={"file": ("doc.pdf", b"%PDF", "application/pdf")}
    )

    assert resp.status_code == 400


def test_the_coordinator_error_keeps_its_status_code(admin_client, ocr_transport):
    # Un 503 por libzbar ausente no es lo mismo que una foto ilegible.
    ocr_transport(lambda _: httpx.Response(503, json={"detail": "falta libzbar en esta imagen"}))

    resp = admin_client.post(
        "/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "barcode"}
    )

    assert resp.status_code == 503
    assert "libzbar" in resp.json()["detail"]


def test_an_unreachable_coordinator_gives_502(admin_client, ocr_transport):
    def down(request):
        raise httpx.ConnectError("connection refused")

    ocr_transport(down)

    resp = admin_client.post("/admin/ocr/scan", files={"file": PHOTO})

    assert resp.status_code == 502


def test_purge_goes_through_the_coordinator(admin_client, ocr_transport):
    # Contra el coordinador y no contra el storage: el storage es dependencia
    # suya, no de este servicio.
    received = ocr_transport(lambda _: httpx.Response(200, json={"purged": 3}))

    data = admin_client.post("/admin/ocr/temp/purge").json()

    assert data == {"purged": 3}
    assert received[0].url.path == "/ocr/maintenance/purge-temp"


def test_the_endpoints_require_an_admin():
    with TestClient(app) as c:
        assert c.get("/admin/ocr/status").status_code in (401, 403)


# ── auditoría de ejecuciones ─────────────────────────────────────────────────

def test_de_cada_escaneo_queda_un_renglon_breve(admin_client, ocr_transport):
    # La foto se descarta; sin este renglón no quedaría rastro de que el módulo
    # corrió, ni de quién lo usó.
    ocr_transport(lambda _: httpx.Response(200, json=READING))

    admin_client.post("/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "barcode"})
    pagina = admin_client.get("/admin/ocr/audit").json()

    assert pagina["total"] == 1
    entrada = pagina["items"][0]
    assert entrada["user_email"] == FakeAdmin.email
    assert entrada["origen"] == "panel"
    assert entrada["doc_type"] == "barcode"
    assert entrada["engine"] == "zbar"
    assert entrada["resumen"] == "7794640172021 (EAN-13)"
    assert entrada["processing_ms"] == 129


def test_el_resumen_se_recorta_y_nunca_guarda_el_documento_entero(
    admin_client, ocr_transport, db_session
):
    largo = dict(READING)
    largo["result"] = dict(READING["result"], texto_legible="RENGLON DE TICKET " * 60)
    ocr_transport(lambda _: httpx.Response(200, json=largo))

    admin_client.post("/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "receipt"})
    resumen = admin_client.get("/admin/ocr/audit").json()["items"][0]["resumen"]

    assert len(resumen) <= ocr_audit.RESUMEN_MAXIMO
    assert resumen.endswith("…")


def test_las_estadisticas_separan_por_tipo_de_documento(admin_client, db_session):
    # Mezclarlos no significa nada: un código vuelve en dos décimas y siempre
    # acierta; un comprobante tarda un minuto y siempre necesita revisión.
    db_session.add_all([
        OCRScanAudit(user_email="a@x.com", origen="app", doc_type="barcode",
                     status="done", processing_ms=120, confianza=1.0),
        OCRScanAudit(user_email="b@x.com", origen="app", doc_type="barcode",
                     status="done", processing_ms=180, confianza=1.0),
        OCRScanAudit(user_email="b@x.com", origen="app", doc_type="receipt",
                     status="failed", processing_ms=60000, confianza=0.4),
    ])
    db_session.commit()

    datos = admin_client.get("/admin/ocr/stats?dias=1").json()

    assert (datos["ejecuciones"], datos["exitosas"], datos["fallidas"]) == (3, 2, 1)
    assert datos["usuarios"] == 2

    por_tipo = {t["doc_type"]: t for t in datos["por_tipo"]}
    assert por_tipo["barcode"]["ms_promedio"] == 150
    assert por_tipo["barcode"]["confianza_promedio"] == 1.0
    assert por_tipo["receipt"]["fallidas"] == 1


def test_la_auditoria_se_puede_filtrar_por_usuario(admin_client, db_session):
    db_session.add_all([
        OCRScanAudit(user_email="hogar@x.com", origen="app", doc_type="barcode", status="done"),
        OCRScanAudit(user_email="comercio@x.com", origen="app", doc_type="receipt", status="done"),
    ])
    db_session.commit()

    pagina = admin_client.get("/admin/ocr/audit?user=comercio").json()

    assert pagina["total"] == 1
    assert pagina["items"][0]["doc_type"] == "receipt"


def test_un_fallo_del_modulo_no_se_lleva_puesto_al_escaneo(
    admin_client, ocr_transport, monkeypatch
):
    # Perder el resultado de una foto por no poder escribir un renglón de
    # historial sería un mal negocio para quien está esperando.
    ocr_transport(lambda _: httpx.Response(200, json=READING))
    monkeypatch.setattr(ocr_audit, "_ya_registrado", lambda *a, **k: False)
    monkeypatch.setattr(
        ocr_audit, "OCRScanAudit",
        lambda **kwargs: (_ for _ in ()).throw(SQLAlchemyError("tabla caída")),
    )

    resp = admin_client.post("/admin/ocr/scan", files={"file": PHOTO})

    assert resp.status_code == 200
    assert resp.json()["result"]["engine"] == "zbar"


def test_un_escaneo_del_panel_que_falla_queda_en_la_auditoria(admin_client, ocr_transport, db_session):
    ocr_transport(lambda _: httpx.Response(502, json={"detail": "El motor no respondió"}))

    resp = admin_client.post("/admin/ocr/scan", files={"file": PHOTO}, data={"doc_type": "receipt"})

    assert resp.status_code == 502
    fila = db_session.query(OCRScanAudit).filter(OCRScanAudit.origen == "panel").one()
    assert fila.status == "failed"
    assert "motor" in fila.resumen
