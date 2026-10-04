"""Shared CSV-export hygiene.

CSV formula injection (OWASP): free-text fields (trade names, supplier and
customer names, phones) carry no character restriction, and the role that can
create them is often narrower than the role that can export (reports.export) —
a value starting with =, +, -, or @ would be interpreted as a formula by
Excel/Sheets on open. Prefixing with a single quote is the standard
mitigation; spreadsheet apps render it as forced-text and drop the quote from
display. Every CSV export that includes free-text fields MUST route them
through csv_safe().
"""

__all__ = ["csv_safe"]


def csv_safe(value: str | None) -> str:
    return f"'{value}" if value and value[0] in "=+-@" else (value or "")
