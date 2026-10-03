"""
El flujo de carga por foto del usuario.

Lo que se prueba acá no es que el OCR lea bien —eso se prueba en `ocr_service`,
contra fotos reales— sino lo que este servicio agrega encima: emparejar la
lectura con el inventario de quien escanea, y que el movimiento resultante quede
bien asentado.
"""
import httpx
import pytest

from app.api import me_ocr
from app.core import matching, ocr_gateway
from app.core.security import get_bearer_token
from app.main import app
from app.models.app_models import ConsumptionEvent
from app.models.audit import OCRScanAudit
from tests.conftest import create_product

TOKEN = "token-del-usuario"
FOTO = ("foto.jpg", b"\xff\xd8foto-de-prueba", "image/jpeg")


def lectura(fields=None, items=None, **extra):
    """Un OCRResult como el que devuelve el coordinador."""
    base = {
        "fields": fields or {},
        "items": items or [],
        "document_confidence": 0.9,
        "needs_review": False,
        "engine": "hybrid",
        "texto_legible": "texto",
        "warnings": [],
    }
    base.update(extra)
    return base


def campo(valor, confianza=0.9):
    return {"value": valor, "confidence": confianza}


@pytest.fixture()
def user_client(client):
    """El cliente de `conftest`, con el bearer que se reenvía al coordinador."""
    app.dependency_overrides[get_bearer_token] = lambda: TOKEN
    return client


@pytest.fixture()
def coordinador(monkeypatch):
    """Instala un coordinador de mentira y devuelve las peticiones que recibió."""
    def install(handler):
        recibidas = []

        def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            return handler(request)

        class FakeClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(registrar)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(ocr_gateway.httpx, "AsyncClient", FakeClient)
        return recibidas

    return install


# ── emparejar nombres ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "leido, guardado, minimo",
    [
        ("Leche entera", "leche entera", 1.0),          # sólo cambia el caso
        ("CAFE LA VIRGINIA", "Café La Virginia", 1.0),  # sólo cambian los acentos
        ("YOG SER FRUT", "Yogur Ser frutilla", 0.9),    # el ticket abrevia
        ("LECHE", "Leche entera descremada", 0.9),      # el ticket recorta
    ],
)
def test_el_mismo_producto_escrito_distinto_se_reconoce(leido, guardado, minimo):
    assert matching.parecido(leido, guardado) >= minimo


def test_dos_productos_distintos_no_se_confunden():
    assert matching.parecido("Leche entera", "Detergente") < matching.UMBRAL_CANDIDATO


# ── código de barras ─────────────────────────────────────────────────────────

def test_un_codigo_de_barras_vuelve_resuelto_y_atado_a_su_producto(
    user_client, coordinador, db_session, auth_user
):
    # No se encola: se resuelve en la misma llamada, porque no pasa por un motor.
    producto = create_product(db_session, auth_user, nombre="Ibuprofeno", codigo_barras="7794640172021")
    recibidas = coordinador(lambda _: httpx.Response(200, json={
        "doc_type": "barcode",
        "result": lectura({"codigo": campo("7794640172021", 1.0),
                           "pais_registro": campo("Argentina", 1.0)}),
    }))

    resp = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                            data={"doc_type": "barcode"})

    assert resp.status_code == 200
    cuerpo = resp.json()
    assert cuerpo["status"] == "done"
    assert recibidas[0].url.path == "/ocr/sync/upload"

    draft = cuerpo["result"]["draft"]
    assert draft["codigo_barras"] == "7794640172021"
    assert draft["coincidencia_id"] == producto.id
    assert draft["coincidencias"][0]["nombre"] == "Ibuprofeno"
    assert draft["coincidencias"][0]["por_codigo"] is True


def test_el_codigo_de_barras_tambien_deja_su_renglon_de_auditoria(
    user_client, coordinador, db_session, auth_user
):
    # No tiene job porque no se encola, pero es una ejecución del módulo igual
    # que las otras y el historial tiene que contarla.
    coordinador(lambda _: httpx.Response(200, json={
        "doc_type": "barcode",
        "result": lectura({"codigo": campo("7794640172021", 1.0)}, engine="zbar"),
    }))

    user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                     data={"doc_type": "barcode"})

    fila = db_session.query(OCRScanAudit).filter(
        OCRScanAudit.user_email == auth_user.email,
        OCRScanAudit.doc_type == "barcode",
    ).one()
    assert fila.job_id is None
    assert fila.engine == "zbar"
    assert fila.origen == "app"


def test_un_codigo_desconocido_vuelve_sin_coincidencias_pero_con_el_codigo(
    user_client, coordinador, db_session, auth_user
):
    # Es lo que le dice al formulario que abra un alta nueva con el código puesto.
    create_product(db_session, auth_user, nombre="Ibuprofeno", codigo_barras="7794640172021")
    coordinador(lambda _: httpx.Response(200, json={
        "doc_type": "barcode",
        "result": lectura({"codigo": campo("7790895000997", 1.0)}),
    }))

    draft = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                             data={"doc_type": "barcode"}).json()["result"]["draft"]

    assert draft["codigo_barras"] == "7790895000997"
    assert draft["coincidencias"] == []
    assert draft["coincidencia_id"] is None


# ── etiqueta de producto ─────────────────────────────────────────────────────

def test_una_etiqueta_se_encola_y_no_espera(user_client, coordinador):
    recibidas = coordinador(lambda _: httpx.Response(202, json={"job_id": "j-1", "status": "queued"}))

    resp = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                            data={"doc_type": "product_label"})

    assert resp.status_code == 202
    assert resp.json() == {
        "job_id": "j-1", "status": "queued", "doc_type": "product_label",
        "result": None, "error": None,
        "queue_position": None, "estimated_wait_seconds": None,
    }
    assert recibidas[0].url.path == "/ocr/scan"


def test_mientras_espera_dice_que_lugar_ocupa_en_la_fila(user_client, coordinador):
    # Sin esto la pantalla sólo puede girar; con esto puede decir cuánto falta.
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-1", "status": "queued", "doc_type": "product_label",
        "result": None, "created_at": "", "updated_at": "",
        "queue_position": 3, "estimated_wait_seconds": 57,
    }))

    cuerpo = user_client.get("/me/products/ocr/scan/j-1").json()

    assert cuerpo["queue_position"] == 3
    assert cuerpo["estimated_wait_seconds"] == 57
    assert cuerpo["result"] is None


def test_la_etiqueta_terminada_se_empareja_con_lo_que_el_usuario_ya_tiene(
    user_client, coordinador, db_session, auth_user
):
    producto = create_product(db_session, auth_user, nombre="Yogur Ser", marca="Ser")
    create_product(db_session, auth_user, nombre="Detergente", marca="Ala")
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-1", "status": "done", "doc_type": "product_label",
        "created_at": "", "updated_at": "",
        "result": lectura({"nombre_producto": campo("YOGUR SER"), "marca": campo("Ser")}),
    }))

    draft = user_client.get("/me/products/ocr/scan/j-1").json()["result"]["draft"]

    assert draft["nombre"] == "YOGUR SER"
    assert draft["coincidencia_id"] == producto.id
    assert [c["nombre"] for c in draft["coincidencias"]] == ["Yogur Ser"]


def test_sin_coincidencias_vuelve_el_texto_leido_para_editarlo(
    user_client, coordinador, db_session, auth_user
):
    create_product(db_session, auth_user, nombre="Detergente")
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-1", "status": "done", "doc_type": "product_label",
        "created_at": "", "updated_at": "",
        "result": lectura({"nombre_producto": campo("Arroz largo fino doble carolina")}),
    }))

    resultado = user_client.get("/me/products/ocr/scan/j-1").json()["result"]
    draft = resultado["draft"]

    # El nombre entra recortado en el formulario, pero el texto completo viaja
    # aparte: recortar no puede ser la única versión de lo que se leyó.
    assert draft["coincidencias"] == []
    assert draft["texto_leido"] == "Arroz largo fino doble carolina"
    assert len(draft["nombre"]) <= me_ocr.MAX_NOMBRE
    assert "nombre_recortado" in resultado["advertencias"]


# ── comprobante ──────────────────────────────────────────────────────────────

def test_un_comprobante_empareja_cada_renglon_y_cuenta_los_que_quedan_afuera(
    user_client, coordinador, db_session, auth_user
):
    create_product(db_session, auth_user, nombre="Leche entera")
    create_product(db_session, auth_user, nombre="Yogur Ser")
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-1", "status": "done", "doc_type": "receipt",
        "created_at": "", "updated_at": "",
        "result": lectura(
            {"comercio": campo("Dia"), "total": campo(16015.25)},
            items=[
                {"nombre_producto": campo("LECHE ENTERA"), "cantidad": campo(2)},
                {"nombre_producto": campo("YOG SER"), "cantidad": campo(1)},
                {"nombre_producto": campo("ESPONJA COCINA"), "cantidad": campo(3)},
            ],
        ),
    }))

    resultado = user_client.get("/me/products/ocr/scan/j-1").json()["result"]

    assert resultado["comprobante"]["comercio"] == "Dia"
    assert [i["stock"] for i in resultado["items"]] == [2, 1, 3]
    assert [bool(i["coincidencias"]) for i in resultado["items"]] == [True, True, False]
    # Es el número del aviso "no se pudieron cargar N productos".
    assert resultado["sin_coincidencia"] == 1


# ── salida por comprobante ───────────────────────────────────────────────────

def test_la_salida_por_comprobante_descarga_y_deja_asentado_el_motivo(
    user_client, db_session, auth_user
):
    leche = create_product(db_session, auth_user, nombre="Leche entera", stock=10)
    yogur = create_product(db_session, auth_user, nombre="Yogur Ser", stock=4)

    resp = user_client.post("/me/products/ocr/salida", json={
        "items": [{"product_id": leche.id, "quantity": 2},
                  {"product_id": yogur.id, "quantity": 1}],
        "comercio": "Dia",
    })

    assert resp.status_code == 200
    assert resp.json() == {"procesados": 2, "fallidos": 0, "errores": []}

    db_session.refresh(leche)
    db_session.refresh(yogur)
    assert (leche.stock, yogur.stock) == (8, 3)

    eventos = db_session.query(ConsumptionEvent).filter(
        ConsumptionEvent.user_id == str(auth_user.id)
    ).all()
    # Para el ledger y las métricas es un consumo como cualquier otro; lo que lo
    # distingue es el motivo, que es lo que después se muestra en el historial.
    assert {e.event_type for e in eventos} == {"consumed"}
    assert all(e.note.startswith(me_ocr.MOTIVO_COMPROBANTE) for e in eventos)
    assert "Dia" in eventos[0].note


def test_un_renglon_sin_stock_no_arrastra_al_resto(user_client, db_session, auth_user):
    leche = create_product(db_session, auth_user, nombre="Leche entera", stock=10)
    yogur = create_product(db_session, auth_user, nombre="Yogur Ser", stock=1)

    cuerpo = user_client.post("/me/products/ocr/salida", json={
        "items": [{"product_id": leche.id, "quantity": 2},
                  {"product_id": yogur.id, "quantity": 5}],
    }).json()

    assert cuerpo["procesados"] == 1
    assert cuerpo["fallidos"] == 1
    assert str(yogur.id) in cuerpo["errores"][0]

    db_session.refresh(leche)
    db_session.refresh(yogur)
    assert (leche.stock, yogur.stock) == (8, 1)


def test_no_se_puede_descargar_un_producto_ajeno(user_client, db_session, auth_user):
    ajeno = create_product(db_session, auth_user, nombre="De otro", stock=5,
                           user_id="00000000-0000-0000-0000-000000000000")

    cuerpo = user_client.post("/me/products/ocr/salida", json={
        "items": [{"product_id": ajeno.id, "quantity": 1}],
    }).json()

    assert cuerpo["procesados"] == 0
    assert "no encontrado" in cuerpo["errores"][0].lower()
    db_session.refresh(ajeno)
    assert ajeno.stock == 5


# ── búsqueda por código, sin foto ────────────────────────────────────────────

def test_buscar_por_codigo_encuentra_y_normaliza(user_client, db_session, auth_user):
    producto = create_product(db_session, auth_user, nombre="Ibuprofeno",
                              codigo_barras="7794640172021")

    # Escrito con separadores, como lo transcribe una persona mirando el envase.
    cuerpo = user_client.get("/me/products/by-barcode/779-464 017 2021").json()

    assert cuerpo["id"] == producto.id


def test_un_codigo_que_no_esta_devuelve_nulo_y_no_un_error(user_client, db_session, auth_user):
    # Que no esté es un resultado normal: es lo que abre el alta con el código puesto.
    create_product(db_session, auth_user, nombre="Ibuprofeno", codigo_barras="7794640172021")

    resp = user_client.get("/me/products/by-barcode/7790895000997")

    assert resp.status_code == 200
    assert resp.json() is None


# ── credenciales ─────────────────────────────────────────────────────────────

def test_al_coordinador_va_el_token_del_usuario_y_no_uno_de_servicio(user_client, coordinador):
    # El coordinador cuenta la cuota por minuto y guarda el dueño del job según el
    # `sub` del token: con un token de servicio todos compartirían cupo y escaneos.
    recibidas = coordinador(lambda _: httpx.Response(202, json={"job_id": "j-1", "status": "queued"}))

    user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                     data={"doc_type": "product_label"})

    assert recibidas[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_el_limite_por_minuto_llega_al_usuario_con_su_espera(user_client, coordinador):
    # Sin el `Retry-After` el usuario ve "esperá" sin saber cuánto, que es
    # justamente el dato que el coordinador se tomó el trabajo de calcular.
    coordinador(lambda _: httpx.Response(
        429, json={"detail": "Superaste el límite de escaneos por minuto"},
        headers={"Retry-After": "37"},
    ))

    resp = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                            data={"doc_type": "receipt"})

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "37"


def test_un_formato_no_soportado_se_rechaza_antes_de_la_red(user_client, coordinador):
    recibidas = coordinador(lambda _: httpx.Response(200, json={}))

    resp = user_client.post("/me/products/ocr/scan",
                            files={"file": ("doc.pdf", b"%PDF", "application/pdf")})

    assert resp.status_code == 400
    assert recibidas == []


# ── auditoría ────────────────────────────────────────────────────────────────

def test_el_polling_deja_un_solo_renglon_por_job(user_client, coordinador, db_session, auth_user):
    # La pantalla consulta cada medio segundo desde que encola hasta que termina:
    # sin el corte por `job_id` habría un renglón de historial por consulta.
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-42", "status": "done", "doc_type": "product_label",
        "created_at": "", "updated_at": "",
        "result": lectura({"nombre_producto": campo("Leche entera")}),
    }))

    for _ in range(3):
        user_client.get("/me/products/ocr/scan/j-42")

    filas = db_session.query(OCRScanAudit).filter(OCRScanAudit.job_id == "j-42").all()
    assert len(filas) == 1
    assert filas[0].user_email == auth_user.email
    assert filas[0].origen == "app"
    assert filas[0].doc_type == "product_label"


def test_un_escaneo_fallido_tambien_queda_asentado(user_client, coordinador, db_session):
    # Es el caso que más importa auditar: sin la foto ni el renglón, una lectura
    # que falló es indistinguible de una que nunca se pidió.
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-43", "status": "failed", "doc_type": "receipt",
        "created_at": "", "updated_at": "", "result": None,
        "error": "El motor no respondió",
    }))

    user_client.get("/me/products/ocr/scan/j-43")

    fila = db_session.query(OCRScanAudit).filter(OCRScanAudit.job_id == "j-43").one()
    assert fila.status == "failed"
    assert fila.resumen == "El motor no respondió"


def test_mientras_espera_en_la_cola_todavia_no_se_audita(user_client, coordinador, db_session):
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-44", "status": "queued", "doc_type": "receipt",
        "created_at": "", "updated_at": "", "result": None, "queue_position": 2,
    }))

    user_client.get("/me/products/ocr/scan/j-44")

    assert db_session.query(OCRScanAudit).filter(OCRScanAudit.job_id == "j-44").count() == 0


# ── huecos cerrados en la auditoría de la integración ─────────────────────────

def test_con_varios_lotes_del_mismo_codigo_va_primero_el_que_vence_antes(
    user_client, coordinador, db_session, auth_user
):
    # Sin ordenar, la salida por código descontaba de un lote cualquiera.
    from datetime import date

    tardio = create_product(db_session, auth_user, nombre="Ibuprofeno", stock=4,
                            codigo_barras="7794640172021", vencimiento=date(2028, 6, 1))
    sin_fecha = create_product(db_session, auth_user, nombre="Ibuprofeno", stock=4,
                               codigo_barras="7794640172021", vencimiento=None)
    pronto = create_product(db_session, auth_user, nombre="Ibuprofeno", stock=4,
                            codigo_barras="7794640172021", vencimiento=date(2026, 12, 1))
    coordinador(lambda _: httpx.Response(200, json={
        "doc_type": "barcode", "result": lectura({"codigo": campo("7794640172021", 1.0)}),
    }))

    draft = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                             data={"doc_type": "barcode"}).json()["result"]["draft"]

    assert [c["id"] for c in draft["coincidencias"]] == [pronto.id, tardio.id, sin_fecha.id]
    assert draft["coincidencia_id"] == pronto.id


def test_un_codigo_que_falla_en_el_modulo_tambien_se_audita(user_client, coordinador, db_session, auth_user):
    # Sin esto las "fallidas" del panel sólo contaban los trabajos encolados.
    coordinador(lambda _: httpx.Response(503, json={"detail": "Decodificador no disponible"}))

    resp = user_client.post("/me/products/ocr/scan", files={"file": FOTO}, data={"doc_type": "barcode"})

    assert resp.status_code == 503
    fila = db_session.query(OCRScanAudit).filter(OCRScanAudit.user_email == auth_user.email).one()
    assert (fila.status, fila.doc_type) == ("failed", "barcode")
    assert "Decodificador" in fila.resumen


def test_un_rechazo_por_cupo_no_cuenta_como_ejecucion(user_client, coordinador, db_session, auth_user):
    # Un 429 no llegó a correr nada: contarlo inflaría las fallidas del OCR.
    coordinador(lambda _: httpx.Response(429, json={"detail": "Cupo agotado"}, headers={"Retry-After": "30"}))

    user_client.post("/me/products/ocr/scan", files={"file": FOTO}, data={"doc_type": "barcode"})

    assert db_session.query(OCRScanAudit).filter(OCRScanAudit.user_email == auth_user.email).count() == 0


# ── sello de vencimiento ─────────────────────────────────────────────────────

def test_un_sello_de_vencimiento_se_encola_y_se_traduce(user_client, coordinador):
    coordinador(lambda _: httpx.Response(202, json={"job_id": "j-venc", "status": "queued"}))

    resp = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                            data={"doc_type": "expiry_date"})

    assert resp.status_code == 202
    assert resp.json()["doc_type"] == "expiry_date"


def test_un_sello_de_vencimiento_terminado_trae_la_fecha(user_client, coordinador):
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-venc", "status": "done", "doc_type": "expiry_date",
        "created_at": "", "updated_at": "",
        "result": lectura({"fecha_vencimiento": campo("2026-12-31")}),
    }))

    draft = user_client.get("/me/products/ocr/scan/j-venc").json()["result"]["draft"]

    assert draft["vencimiento"] == "2026-12-31"


def test_una_fecha_con_formato_invalido_queda_vacia_en_el_borrador(user_client, coordinador):
    # El coordinador siempre entrega ISO; si algo raro llega igual, mejor un
    # borrador con el campo vacío para completar a mano que uno que no carga.
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-venc-mal", "status": "done", "doc_type": "expiry_date",
        "created_at": "", "updated_at": "",
        "result": lectura({"fecha_vencimiento": campo("31 de diciembre")}),
    }))

    draft = user_client.get("/me/products/ocr/scan/j-venc-mal").json()["result"]["draft"]

    assert draft["vencimiento"] is None


# ── comprobante: renglones sin nombre ────────────────────────────────────────

def test_un_renglon_de_comprobante_sin_nombre_se_descarta(user_client, coordinador, db_session, auth_user):
    coordinador(lambda _: httpx.Response(200, json={
        "job_id": "j-recibo", "status": "done", "doc_type": "receipt",
        "created_at": "", "updated_at": "",
        "result": lectura(
            {"comercio": campo("Dia")},
            items=[
                {"nombre_producto": {"value": None, "confidence": 0.0}, "cantidad": campo(1)},
                {"nombre_producto": campo("Leche entera"), "cantidad": campo(1)},
            ],
        ),
    }))

    resultado = user_client.get("/me/products/ocr/scan/j-recibo").json()["result"]

    assert len(resultado["items"]) == 1
    assert resultado["items"][0]["nombre"] == "Leche entera"


# ── código de barras no leído ────────────────────────────────────────────────

def test_un_codigo_de_barras_no_leido_no_rompe_el_emparejado(user_client, coordinador, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Ibuprofeno", codigo_barras="7794640172021")
    coordinador(lambda _: httpx.Response(200, json={
        "doc_type": "barcode", "result": lectura({}),  # sin campo "codigo"
    }))

    draft = user_client.post("/me/products/ocr/scan", files={"file": FOTO},
                             data={"doc_type": "barcode"}).json()["result"]["draft"]

    assert draft["coincidencias"] == []
    assert draft["coincidencia_id"] is None


# ── salida por comprobante: fallo al persistir ───────────────────────────────

def test_la_salida_por_comprobante_informa_un_fallo_de_base_sin_frenar_el_resto(
    user_client, db_session, auth_user, monkeypatch
):
    from app.api import me_ocr as me_ocr_api

    leche = create_product(db_session, auth_user, nombre="Leche entera", stock=10)

    original_commit = db_session.commit
    llamadas = {"n": 0}

    def commit_que_falla_la_primera_vez():
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError("no se pudo escribir")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", commit_que_falla_la_primera_vez)

    resp = user_client.post("/me/products/ocr/salida", json={
        "items": [{"product_id": leche.id, "quantity": 2}],
    })

    cuerpo = resp.json()
    assert cuerpo["procesados"] == 0
    assert cuerpo["fallidos"] == 1
    assert "no se pudo registrar la salida" in cuerpo["errores"][0]
