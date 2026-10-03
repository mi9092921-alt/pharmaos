"""GS1 DataMatrix parser (Egypt EDA track & trace — decrees 161/475 of 2025).

Every pack carries a 2D GS1 DataMatrix encoding, at minimum:
  AI 01 = GTIN (14, fixed)      AI 17 = expiry YYMMDD (6, fixed)
  AI 10 = batch/lot (variable)  AI 21 = serial (variable)

Variable-length AIs are terminated by the FNC1/GS separator (ASCII 29) or end
of data. HID scanners commonly prefix the symbology identifier "]d2".

Expiry rules (GS1 GenSpec): day "00" means end of month; years 51..99 are 19xx
(irrelevant for pharma in practice but implemented per spec), else 20xx.
"""

import calendar
import datetime as dt
from dataclasses import dataclass

GS = "\x1d"  # FNC1 group separator as transmitted by HID scanners

# AI -> fixed length (None = variable, GS-terminated)
_AI_TABLE: dict[str, int | None] = {"01": 14, "17": 6, "10": None, "21": None}
_MAX_VARIABLE = 20


class Gs1ParseError(ValueError):
    """The code is not a parseable GS1 element string."""


@dataclass(frozen=True)
class Gs1Pack:
    gtin: str | None = None
    expiry_date: dt.date | None = None
    batch_number: str | None = None
    serial_number: str | None = None


def _parse_expiry(raw: str) -> dt.date:
    if len(raw) != 6 or not raw.isdigit():
        raise Gs1ParseError(f"invalid expiry field: {raw!r}")
    yy, mm, dd = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    year = 1900 + yy if 51 <= yy <= 99 else 2000 + yy
    if not 1 <= mm <= 12:
        raise Gs1ParseError(f"invalid expiry month: {raw!r}")
    if dd == 0:  # GS1: day 00 = last day of the month
        dd = calendar.monthrange(year, mm)[1]
    try:
        return dt.date(year, mm, dd)
    except ValueError as exc:
        raise Gs1ParseError(f"invalid expiry date: {raw!r}") from exc


def parse_gs1(data: str) -> Gs1Pack:
    """Parse a GS1 element string into the pack identifiers.

    Accepts optional "]d2" symbology prefix and a leading FNC1. AIs may appear
    in any order; unknown AIs abort with Gs1ParseError (defensive: decree
    804/2025 treats malformed codes as non-compliant product).
    """
    if data.startswith("]d2") or data.startswith("]Q3"):
        data = data[3:]
    data = data.lstrip(GS)
    if not data:
        raise Gs1ParseError("empty code")

    fields: dict[str, str] = {}
    i = 0
    while i < len(data):
        ai = data[i : i + 2]
        length = _AI_TABLE.get(ai)
        if ai not in _AI_TABLE:
            raise Gs1ParseError(f"unsupported AI {ai!r} at position {i}")
        i += 2
        if length is not None:
            value = data[i : i + length]
            if len(value) != length:
                raise Gs1ParseError(f"truncated AI {ai}")
            i += length
        else:
            end = data.find(GS, i)
            value = data[i:] if end == -1 else data[i:end]
            if not value or len(value) > _MAX_VARIABLE:
                raise Gs1ParseError(f"bad variable AI {ai} length")
            i = len(data) if end == -1 else end
        if ai in fields:
            raise Gs1ParseError(f"repeated AI {ai}")
        fields[ai] = value
        while i < len(data) and data[i] == GS:  # skip separators
            i += 1

    gtin = fields.get("01")
    if gtin is not None and (len(gtin) != 14 or not gtin.isdigit()):
        raise Gs1ParseError(f"invalid GTIN: {gtin!r}")
    return Gs1Pack(
        gtin=gtin,
        expiry_date=_parse_expiry(fields["17"]) if "17" in fields else None,
        batch_number=fields.get("10"),
        serial_number=fields.get("21"),
    )
