import httpx

def error_detail(response: httpx.Response):
    """El `detail` del error de otro servicio, sin el JSON que lo envuelve.

    Los proxies reenviaban `response.text` como `detail`, así que un error de ML
    como `{"detail": "Datos insuficientes"}` le llegaba al panel como el texto
    `{"detail":"Datos insuficientes"}`, con las llaves y las comillas escapadas.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300] or f"El servicio respondió {response.status_code}"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if detail else response.text[:300]
