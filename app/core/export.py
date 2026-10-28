import csv
import io
from typing import Optional

from openpyxl import Workbook

# Las mismas columnas, y en este orden, que pide la carga de archivos: un archivo exportado se puede volver a subir
COLUMNS = [
    "nombre", "stock", "categoria", "marca", "vencimiento", "codigo_barras",
    "descripcion", "ubicacion", "dias_stock_minimo", "dias_alerta_vencimiento",
]


def _row_values(p, owner_email: Optional[str]) -> list:
    # Lo que un producto no tiene va vacío
    values = [
        p.nombre,
        p.stock,
        p.categoria,
        p.marca or "",
        p.vencimiento.isoformat() if p.vencimiento else "",
        p.codigo_barras or "",
        p.descripcion or "",
        p.ubicacion or "",
        p.stock_minimo if p.stock_minimo is not None else "",
        p.dias_alerta_vencimiento if p.dias_alerta_vencimiento is not None else "",
    ]
    if owner_email is not None:
        values.append(owner_email)
    return values


def products_to_csv(products, owner_emails: Optional[dict] = None) -> bytes:
    columns = COLUMNS + (["propietario"] if owner_emails is not None else [])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for p in products:
        owner = owner_emails.get(p.user_id, "") if owner_emails is not None else None
        writer.writerow(_row_values(p, owner))
    return buf.getvalue().encode("utf-8-sig")


def products_to_xlsx(products, owner_emails: Optional[dict] = None) -> bytes:
    columns = COLUMNS + (["propietario"] if owner_emails is not None else [])
    wb = Workbook()
    ws = wb.active
    ws.title = "Productos"
    ws.append(columns)
    for p in products:
        owner = owner_emails.get(p.user_id, "") if owner_emails is not None else None
        ws.append(_row_values(p, owner))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
