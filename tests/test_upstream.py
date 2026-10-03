"""`error_detail`: el mensaje de un error de otro servicio, sin el JSON que lo envuelve."""
import httpx
import pytest

from app.core.upstream import error_detail


def test_un_detail_de_texto_se_devuelve_tal_cual():
    response = httpx.Response(422, json={"detail": "Datos insuficientes para entrenar"})

    assert error_detail(response) == "Datos insuficientes para entrenar"


def test_un_detail_con_la_lista_de_errores_de_validacion_se_conserva():
    detail = [{"loc": ["body", "stock"], "msg": "x", "type": "missing"}]

    assert error_detail(httpx.Response(422, json={"detail": detail})) == detail


@pytest.mark.parametrize("body", [{"mensaje": "sin detail"}, {"detail": ""}, {"detail": None}, ["una", "lista"]])
def test_un_json_sin_detail_util_devuelve_el_texto_crudo(body):
    response = httpx.Response(500, json=body)

    assert error_detail(response) == response.text[:300]


def test_una_respuesta_que_no_es_json_devuelve_su_texto():
    assert error_detail(httpx.Response(502, text="Bad Gateway")) == "Bad Gateway"


def test_una_respuesta_vacia_dice_que_status_tuvo():
    assert error_detail(httpx.Response(503)) == "El servicio respondió 503"


def test_un_texto_muy_largo_se_recorta():
    assert len(error_detail(httpx.Response(500, text="x" * 1000))) == 300
