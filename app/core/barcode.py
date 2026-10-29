"""
Validación y normalización de códigos de barras de producto.

Se acepta cualquier código de 8, 12, 13 o 14 dígitos. No se exige que cierre el
dígito verificador GS1: muchos productos llevan códigos internos o de tiendas que
no lo cumplen, y rechazarlos impedía cargarlos o buscarlos. El código leído por
el OCR sigue siendo el más confiable porque `ocr_service` sí lo verifica al
decodificar la foto.

Lo que sí se hace es dejarlo en su forma canónica (UPC-A como EAN-13), para que el
mismo envase se encuentre a sí mismo escrito a mano o leído por el escáner.
`app_backend` tiene su propia copia de esta regla, con el mismo criterio.
"""
from typing import Optional

# Largos válidos: EAN-8, UPC-A, EAN-13 y el GTIN-14 de las cajas mayoristas.
LARGOS_VALIDOS = (8, 12, 13, 14)

SEPARADORES = " -"
_DIGITOS = "0123456789"


class CodigoInvalido(ValueError):
    """El código no es solo números o no tiene un largo válido."""


def normalizar(codigo: Optional[str]) -> Optional[str]:
    """Deja el código en su forma canónica, o explica por qué no sirve.

    Se aceptan espacios y guiones porque es como los transcribe una persona
    mirando el envase. No se verifica el dígito verificador: solo que sean
    números y que el largo sea el de un código de barras.
    """
    if codigo is None:
        return None

    texto = str(codigo).strip()
    if not texto:
        return None

    if any(c not in _DIGITOS and c not in SEPARADORES for c in texto):
        raise CodigoInvalido("Un código de barras sólo lleva números.")

    limpio = "".join(c for c in texto if c in _DIGITOS)
    if not limpio:
        return None

    if len(limpio) not in LARGOS_VALIDOS:
        largos = ", ".join(str(n) for n in LARGOS_VALIDOS)
        raise CodigoInvalido(
            f"Un código de barras tiene {largos} dígitos; este tiene {len(limpio)}."
        )

    # UPC-A a EAN-13: es el mismo producto con un cero adelante, y guardarlos de
    # las dos formas haría que el mismo envase no se encuentre a sí mismo.
    return "0" + limpio if len(limpio) == 12 else limpio


def es_valido(codigo: Optional[str]) -> bool:
    try:
        return normalizar(codigo) is not None
    except CodigoInvalido:
        return False
