import unicodedata
from typing import Any, Callable, Optional

from sqlalchemy.orm import Query


def alphabetical_key(text: Optional[str]) -> str:
    """Clave para ordenar texto alfabéticamente sin que importen mayúsculas, minúsculas ni tildes.

    Así "ana", "Ana" y "Álvaro" quedan en el orden de un diccionario y no con todas las
    mayúsculas antes que las minúsculas. La "ñ" se ordena como letra propia, después de la "n",
    como en español.
    """
    folded = (text or "").casefold().replace("\u00f1", "n\uffff")
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def only_sortable(query: Query, sort_by: Optional[str], expiry_column: Any) -> Query:
    """Al ordenar por vencimiento, los productos sin fecha no entran: ni creciente ni decreciente."""
    if sort_by == "vencimiento":
        return query.filter(expiry_column.isnot(None))
    return query


def page_of_products(
    query: Query,
    sort_by: Optional[str],
    sort_dir: str,
    columns: dict,
    newest_first: tuple,
    id_column: Any,
    name_of: Callable[[Any], str],
    offset: int = 0,
    limit: Optional[int] = None,
) -> list:
    """Productos en el orden pedido (una página si hay `limit`, todos si no).

    - Por nombre se ordena alfabéticamente de verdad, sin distinguir mayúsculas ni tildes.
    - El id desempata siempre, así dos páginas seguidas nunca repiten ni se saltean productos.
    - Sin orden pedido salen los más nuevos primero.
    """
    end = offset + limit if limit is not None else None
    if sort_by == "nombre":
        ordered = sorted(
            query.all(),
            key=lambda product: (alphabetical_key(name_of(product)), name_of(product), product.id),
            reverse=sort_dir == "desc",
        )
        return ordered[offset:end]
    if sort_by:
        column = columns[sort_by]
        query = query.order_by(column.desc() if sort_dir == "desc" else column.asc(), id_column.asc())
    else:
        query = query.order_by(*newest_first)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()
