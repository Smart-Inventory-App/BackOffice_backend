import csv
import io
from typing import Optional

from openpyxl import Workbook

COLUMNS = [
    "id", "nombre", "marca", "categoria", "stock", "vencimiento",
    "descripcion", "imagen_url", "ubicacion",
]


def _row_values(p, owner_email: Optional[str]) -> list:
    values = [
        p.id,
        p.nombre,
        p.marca or "",
        p.categoria,
        p.stock,
        p.vencimiento.isoformat() if p.vencimiento else "",
        p.descripcion or "",
        p.imagen_url or "",
        p.ubicacion or "",
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
