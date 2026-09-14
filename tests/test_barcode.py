"""
Validación y normalización del código de barras.

La regla está duplicada a propósito en `app_backend`, con estos mismos casos: si
un servicio acepta un código que el otro rechaza, el mismo envase deja de
encontrarse a sí mismo según desde dónde se lo haya cargado.
"""
import pytest

from app.core import barcode
from app.models.app_models import Product
from tests.conftest import create_product

ALTA = {"nombre": "Ibuprofeno", "categoria": "Farmacia", "stock": 5}


# ── la regla ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "escrito, canonico",
    [
        ("7794640172021", "7794640172021"),      # EAN-13 tal cual
        ("779-464 017 2021", "7794640172021"),   # como lo copia una persona del envase
        ("036000291452", "0036000291452"),       # UPC-A se guarda como EAN-13
        ("96385074", "96385074"),                # EAN-8
        ("10012345678902", "10012345678902"),    # GTIN-14 de una caja mayorista
    ],
)
def test_los_codigos_validos_quedan_en_su_forma_canonica(escrito, canonico):
    assert barcode.normalizar(escrito) == canonico


@pytest.mark.parametrize(
    "escrito",
    [
        "12345",           # ningún GTIN tiene 5 dígitos
        "7794640172020",   # un dígito mal copiado: no cierra el verificador
        "abc",             # antes se descartaba en silencio y quedaba "sin código"
        "7794640I72021",   # una I en lugar de un 1
        "77946401720²1",   # `isdigit()` lo acepta pero `int()` no puede convertirlo
    ],
)
def test_los_codigos_invalidos_se_rechazan(escrito):
    with pytest.raises(barcode.CodigoInvalido):
        barcode.normalizar(escrito)


@pytest.mark.parametrize("escrito", [None, "", "   "])
def test_un_codigo_vacio_es_no_tener_codigo(escrito):
    assert barcode.normalizar(escrito) is None


# ── en el alta y la edición ───────────────────────────────────────────────────

def test_el_alta_rechaza_un_codigo_invalido(client, db_session, auth_user):
    resp = client.post("/me/products", json={**ALTA, "codigo_barras": "12345"})

    assert resp.status_code == 400
    assert db_session.query(Product).filter(Product.user_id == str(auth_user.id)).count() == 0


def test_el_alta_rechaza_letras_en_lugar_de_ignorarlas(client, db_session, auth_user):
    # Descartarlas en silencio dejaría el producto sin código y el usuario
    # creería haberlo cargado.
    resp = client.post("/me/products", json={**ALTA, "codigo_barras": "abc"})

    assert resp.status_code == 400
    assert "números" in resp.json()["detail"]


def test_el_alta_guarda_el_codigo_normalizado(client, db_session, auth_user):
    resp = client.post("/me/products", json={**ALTA, "codigo_barras": "036000291452"})

    assert resp.status_code == 201
    assert resp.json()["codigo_barras"] == "0036000291452"


def test_la_edicion_rechaza_un_codigo_invalido(client, db_session, auth_user):
    producto = create_product(db_session, auth_user)

    resp = client.patch(f"/me/products/{producto.id}", json={"codigo_barras": "7794640172020"})

    assert resp.status_code == 400


def test_la_edicion_permite_quitar_el_codigo(client, db_session, auth_user):
    producto = create_product(db_session, auth_user, codigo_barras="7794640172021")

    resp = client.patch(f"/me/products/{producto.id}", json={"codigo_barras": ""})

    assert resp.status_code == 200
    db_session.refresh(producto)
    assert producto.codigo_barras is None


def test_el_merge_completa_un_codigo_que_faltaba(client, db_session, auth_user):
    # Es dato nuevo sobre un producto que ya existía, no un producto distinto.
    producto = create_product(db_session, auth_user, nombre="Ibuprofeno", marca=None,
                              categoria="Farmacia", codigo_barras=None)

    resp = client.post("/me/products", json={**ALTA, "codigo_barras": "7794640172021"})

    assert resp.json()["merged"] is True
    db_session.refresh(producto)
    assert producto.codigo_barras == "7794640172021"


def test_el_merge_no_pisa_un_codigo_que_ya_estaba(client, db_session, auth_user):
    producto = create_product(db_session, auth_user, nombre="Ibuprofeno", marca=None,
                              categoria="Farmacia", codigo_barras="7794640172021")

    client.post("/me/products", json={**ALTA, "codigo_barras": "7790895000997"})

    db_session.refresh(producto)
    assert producto.codigo_barras == "7794640172021"
