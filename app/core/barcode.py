"""
Validación y normalización de códigos de barras de producto.

Un GTIN no es un número cualquiera: trae su propio dígito verificador, así que se
puede saber si está bien escrito sin consultar nada. Eso es lo que permite
rechazar un código mal tipeado en el alta manual, y es la razón por la que el
código leído por el OCR es la entrada más confiable de las cuatro.

El mismo criterio corre en `ocr_service`, que es quien decodifica la foto; acá se
repite porque el usuario también puede escribirlo a mano y ese camino no pasa por
el módulo de OCR.
"""
from typing import Optional

# Largos válidos: EAN-8, UPC-A, EAN-13 y el GTIN-14 de las cajas mayoristas.
LARGOS_VALIDOS = (8, 12, 13, 14)


class CodigoInvalido(ValueError):
    """El código no tiene el largo o el dígito verificador que corresponde."""


def _digito_verificador(cuerpo: str) -> int:
    """Dígito verificador GS1 (módulo 10).

    Sirve igual para los cuatro largos: los pesos 3 y 1 se alternan **desde la
    derecha**, así que la fórmula no depende de cuántos dígitos tenga.
    """
    total = sum(
        int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(cuerpo))
    )
    return (10 - total % 10) % 10


def normalizar(codigo: Optional[str]) -> Optional[str]:
    """Deja el código en su forma canónica, o explica por qué no sirve.

    Se aceptan espacios y guiones porque es como los transcribe una persona
    mirando el envase. Lo que no se acepta es un código que no cierre: guardarlo
    igual sería peor que rechazarlo, porque después no va a coincidir con lo que
    lea el escáner.
    """
    if codigo is None:
        return None

    limpio = "".join(c for c in str(codigo) if c.isdigit())
    if not limpio:
        return None

    if len(limpio) not in LARGOS_VALIDOS:
        largos = ", ".join(str(n) for n in LARGOS_VALIDOS)
        raise CodigoInvalido(
            f"Un código de barras tiene {largos} dígitos; este tiene {len(limpio)}."
        )

    if _digito_verificador(limpio[:-1]) != int(limpio[-1]):
        raise CodigoInvalido(
            "El dígito verificador no coincide: revisá que el código esté bien copiado."
        )

    # UPC-A a EAN-13: es el mismo producto con un cero adelante, y guardarlos de
    # las dos formas haría que el mismo envase no se encuentre a sí mismo.
    return "0" + limpio if len(limpio) == 12 else limpio


def es_valido(codigo: Optional[str]) -> bool:
    try:
        return normalizar(codigo) is not None
    except CodigoInvalido:
        return False
