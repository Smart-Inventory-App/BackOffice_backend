"""Hora argentina (UTC-3, sin horario de verano).

Los eventos se guardan en UTC, pero el día de un movimiento, el «hoy» de los gráficos y la hora del chequeo diario de
alertas son los de Argentina, igual que en la app.
"""
from datetime import date, datetime, timedelta, timezone

AR_TZ = timezone(timedelta(hours=-3))
UTC_OFFSET_HOURS = 3


def ar_now() -> datetime:
    return datetime.now(AR_TZ)


def ar_today() -> date:
    return ar_now().date()


def ar_day(moment: datetime) -> date:
    """Día calendario argentino de un instante. Sin zona horaria se asume UTC (así los devuelve la base)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(AR_TZ).date()


def ar_hour_to_utc(hour: int) -> int:
    """La hora del chequeo diario se guarda en UTC: las 05:00 de Argentina son las 8."""
    return (hour + UTC_OFFSET_HOURS) % 24


def utc_hour_to_ar(hour: int) -> int:
    return (hour - UTC_OFFSET_HOURS) % 24
