from datetime import date

from .conftest import create_product


def test_suggest_returns_each_batch_separately(client, db_session, auth_user):
    create_product(db_session, auth_user, nombre="Leche", vencimiento=date(2026, 1, 1), stock=4)
    create_product(db_session, auth_user, nombre="Leche", vencimiento=date(2026, 6, 1), stock=7)

    resp = client.get("/me/products/suggest", params={"q": "Leche"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    vencimientos = {item["vencimiento"] for item in body}
    assert vencimientos == {"2026-01-01", "2026-06-01"}
    # ordenado por vencimiento ascendente
    assert body[0]["vencimiento"] == "2026-01-01"
