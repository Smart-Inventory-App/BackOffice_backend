"""
Emparejar un nombre leído por el OCR con los productos que el usuario ya tiene.

El problema es concreto: un ticket imprime `YOG SER FRUT 0%` y en el inventario
ese producto se llama `Yogur Ser frutilla`. Una comparación exacta no los une, y
crear un producto nuevo por cada abreviatura del comercio rompe el inventario en
pedazos que después nadie junta.

Acá no se decide nada: se devuelven candidatos con su puntaje y **el usuario
confirma**. Cuando ningún candidato llega al umbral, el texto crudo del OCR
vuelve tal cual para que lo edite, que es la única salida honesta cuando no se
sabe de qué producto se trata.
"""
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional

# Debajo de esto ni se ofrece: un candidato malo es peor que ninguno, porque
# invita a confirmar sin leer.
UMBRAL_CANDIDATO = 0.55

# A partir de acá se marca como la coincidencia probable y el formulario la
# preselecciona. Sigue siendo una sugerencia: el usuario la puede cambiar.
UMBRAL_CONFIABLE = 0.85

MAX_CANDIDATOS = 5


def normalizar(texto: Optional[str]) -> str:
    """Minúsculas, sin acentos y con los espacios colapsados.

    El OCR y el usuario escriben el mismo producto de formas que sólo difieren en
    esto, así que normalizar antes de comparar evita que `Café` y `CAFE` cuenten
    como cosas distintas.
    """
    if not texto:
        return ""
    plano = unicodedata.normalize("NFKD", str(texto))
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return " ".join(plano.lower().split())


def _es_abreviatura(corto: str, largo: str) -> bool:
    """¿`corto` es `largo` escrito con menos letras?

    Es el caso típico del ticket: cada palabra abreviada es prefijo de una
    palabra del nombre completo, en cualquier orden y sin repetir. `SequenceMatcher`
    solo castiga mucho estos casos porque compara caracteres seguidos.
    """
    palabras_cortas = corto.split()
    disponibles = largo.split()
    if not palabras_cortas or not disponibles:
        return False

    for palabra in palabras_cortas:
        for i, candidata in enumerate(disponibles):
            if candidata.startswith(palabra):
                disponibles.pop(i)
                break
        else:
            return False
    return True


def parecido(leido: str, guardado: str) -> float:
    """Puntaje de 0 a 1 entre lo que leyó el OCR y un nombre del inventario."""
    a, b = normalizar(leido), normalizar(guardado)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    puntaje = SequenceMatcher(None, a, b).ratio()
    if _es_abreviatura(a, b) or _es_abreviatura(b, a):
        puntaje = max(puntaje, 0.9)
    return puntaje


def puntuar(leido: str, producto) -> float:
    """
    El mejor puntaje del producto contra el texto leído.

    Se prueba también con la marca adelante porque el ticket suele escribirlas
    juntas (`SER YOGUR`) mientras que el inventario las tiene en campos separados.
    """
    puntajes = [parecido(leido, producto.nombre)]
    if producto.marca:
        puntajes.append(parecido(leido, f"{producto.marca} {producto.nombre}"))
        puntajes.append(parecido(leido, f"{producto.nombre} {producto.marca}"))
    return max(puntajes)


def buscar(
    leido: Optional[str],
    productos: Iterable,
    umbral: float = UMBRAL_CANDIDATO,
    maximo: int = MAX_CANDIDATOS,
) -> list[tuple[object, float]]:
    """Los productos del usuario que se parecen al texto leído, de mejor a peor."""
    if not leido:
        return []

    encontrados = [
        (producto, puntaje)
        for producto in productos
        if (puntaje := puntuar(leido, producto)) >= umbral
    ]
    encontrados.sort(key=lambda par: (-par[1], par[0].nombre.lower()))
    return encontrados[:maximo]
