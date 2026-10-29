"""
Validación de productos en el backoffice.

Un producto cargado desde el backoffice tiene que poder editarse y escanearse
desde la app, así que las reglas son las mismas que las del alta de `app_backend`:
stock positivo, nombre y categoría de hasta 20 caracteres, descripción de hasta
400. Antes el backoffice aceptaba todo: un PATCH podía dejar un producto con
stock negativo y el commit masivo sumaba stock al descontar.
"""
import pytest

from app.api.me import _validar_fila
from app.models.app_models import Product
from app.schemas.platform import BulkProductRow
from tests.conftest import create_product

BASE = {"nombre": "Leche", "categoria": "Lacteos", "stock": 5}
CSV_HEADER = "nombre,categoria,stock\n"


def fila(**overrides):
    data = dict(row_number=1, nombre="Harina", categoria="Almacen", stock=3)
    data.update(overrides)
    return data


def commit(client, rows, mode="entrada"):
    resp = client.post("/me/products/bulk/commit", json={"rows": rows, "mode": mode, "origen": "Prueba"})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── alta ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("campo, valor", [
    ("stock", 0),
    ("stock", -5),
    ("nombre", "n" * 21),
    ("categoria", "c" * 21),
    ("descripcion", "d" * 401),
    ("stock_minimo", 0),
])
def test_el_alta_rechaza_datos_que_la_app_tambien_rechaza(client, db_session, campo, valor):
    resp = client.post("/me/products", json={**BASE, campo: valor})

    assert resp.status_code == 422
    assert campo in str(resp.json()["detail"])
    assert db_session.query(Product).count() == 0


def test_el_alta_acepta_los_valores_justo_en_el_limite(client):
    resp = client.post(
        "/me/products",
        json={"nombre": "n" * 20, "categoria": "c" * 20, "stock": 1, "descripcion": "d" * 400},
    )

    assert resp.status_code == 201


# ── edición ──────────────────────────────────────────────────────────────────

def test_editar_con_stock_negativo_se_rechaza_y_no_toca_el_producto(client, db_session, auth_user):
    product = create_product(db_session, auth_user, stock=10)

    resp = client.patch(f"/me/products/{product.id}", json={"stock": -5})

    assert resp.status_code == 422
    db_session.refresh(product)
    assert product.stock == 10


def test_editar_con_stock_cero_se_acepta_porque_es_un_producto_agotado(client, db_session, auth_user):
    """La pantalla de edición permite dejar el stock en 0; lo que no puede haber es stock negativo."""
    product = create_product(db_session, auth_user, stock=10)

    resp = client.patch(f"/me/products/{product.id}", json={"stock": 0})

    assert resp.status_code == 200
    assert resp.json()["stock"] == 0


@pytest.mark.parametrize("campo, valor", [("nombre", "n" * 21), ("categoria", "c" * 21), ("descripcion", "d" * 401)])
def test_editar_con_un_texto_demasiado_largo_se_rechaza(client, db_session, auth_user, campo, valor):
    product = create_product(db_session, auth_user)

    resp = client.patch(f"/me/products/{product.id}", json={campo: valor})

    assert resp.status_code == 422


# ── carga masiva: lectura del archivo ────────────────────────────────────────

def _parse(client, contenido):
    resp = client.post(
        "/me/products/bulk/parse",
        params={"mode": "entrada"},
        files={"file": ("p.csv", contenido.encode("utf-8"), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


def test_el_archivo_marca_como_invalido_un_nombre_de_mas_de_20_caracteres(client):
    rows = _parse(client, CSV_HEADER + "N" * 30 + ",Almacen,2\nOk,Almacen,2\n")

    assert [r["valid"] for r in rows] == [False, True]
    assert rows[0]["errors"] == ["nombre supera los 20 caracteres"]


def test_el_archivo_marca_como_invalida_una_categoria_de_mas_de_20_caracteres(client):
    rows = _parse(client, CSV_HEADER + "Ok," + "C" * 30 + ",2\n")

    assert rows[0]["valid"] is False
    assert rows[0]["errors"] == ["categoria supera los 20 caracteres"]


# ── carga masiva: confirmación ───────────────────────────────────────────────

def test_el_commit_no_confia_en_que_el_cliente_paso_por_el_parse(client, db_session):
    """Es un endpoint más de la API: cada fila se valida de nuevo."""
    result = commit(client, [
        fila(row_number=1),
        fila(row_number=2, stock=-7),
        fila(row_number=3, stock=0),
        fila(row_number=4, nombre="n" * 40),
        fila(row_number=5, nombre="   "),
        fila(row_number=6, categoria=""),
        fila(row_number=7, categoria="c" * 40),
        fila(row_number=8, descripcion="d" * 500),
    ])

    assert result["created"] == 1
    assert result["failed"] == 7
    assert result["errors"] == [
        "Fila 2: El stock debe ser mayor a 0",
        "Fila 3: El stock debe ser mayor a 0",
        "Fila 4: nombre supera los 20 caracteres",
        "Fila 5: El nombre del producto es obligatorio",
        "Fila 6: categoria es obligatoria",
        "Fila 7: categoria supera los 20 caracteres",
        "Fila 8: descripcion supera los 400 caracteres",
    ]
    assert [p.stock for p in db_session.query(Product).all()] == [3]


def test_una_salida_con_cantidad_negativa_no_suma_stock(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Harina", marca=None, stock=3)

    # La primera fila descuenta 1; la segunda, con cantidad negativa, antes habría
    # *sumado* 50 (`stock -= -50`) y ahora se rechaza sin tocar nada.
    result = commit(client, [fila(row_number=1, stock=1, categoria=""), fila(row_number=2, stock=-50, categoria="")], mode="salida")

    assert result["failed"] == 1
    assert result["errors"] == ["Fila 2: El stock debe ser mayor a 0"]
    assert [p["stock"] for p in client.get("/me/products").json()["items"]] == [2]


def test_la_salida_no_exige_categoria(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Harina", marca=None, stock=3)

    result = commit(client, [fila(stock=2, categoria="")], mode="salida")

    assert result["failed"] == 0
    assert [p["stock"] for p in client.get("/me/products").json()["items"]] == [1]


def test_validar_fila_acepta_una_fila_correcta():
    _validar_fila(BulkProductRow(**fila()), "entrada")
    _validar_fila(BulkProductRow(**fila(categoria="")), "salida")
