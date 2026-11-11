"""ESC/POS receipt builder + raw sender (M12 skeleton, completed in P1-M9).

CLAUDE.md printing architecture: 80mm thermal receipts + cash drawer go over
RAW ESC/POS (never browser printing). In production the Electron main process
owns the transport (USB/network); this module provides the byte stream and a
network (port 9100) sender so the slice is testable end-to-end. Printing never
depends on the internet.

Arabic text: sent as UTF-8. Thermal printers vary in Arabic firmware support —
the approved-hardware matrix test (real device, user-side) decides per model
whether native UTF-8, a codepage, or bitmap rendering is used. The byte
protocol below (init/feed/cut/drawer/QR) is model-independent.

Receipt Arabic labels live here by design: the receipt is a printed artifact
for the customer, not an API response (the bilingual-codes rule applies to API
payloads; the M12 skeleton established Arabic labels in this module).
"""

import math
import socket
from dataclasses import dataclass, field
from decimal import Decimal

# Core ESC/POS commands
INIT = b"\x1b@"  # ESC @  — initialize
ALIGN_LEFT = b"\x1ba\x00"
ALIGN_CENTER = b"\x1ba\x01"
ALIGN_RIGHT = b"\x1ba\x02"
BOLD_ON = b"\x1bE\x01"
BOLD_OFF = b"\x1bE\x00"
DOUBLE_SIZE = b"\x1d!\x11"  # GS ! — double width+height
NORMAL_SIZE = b"\x1d!\x00"
FEED = b"\n"
CUT = b"\x1dV\x42\x03"  # GS V B — partial cut with feed
DRAWER_PULSE = b"\x1bp\x00\x19\xfa"  # ESC p — open cash drawer (pin 2)
QR_PREFIX = b"\x1d\x28\x6b"  # GS ( k — QR model/size/EC/store/print family
RASTER_IMAGE_PREFIX = b"\x1d\x76\x30"  # GS v 0 — raster bit-image

_LINE_WIDTH = 42  # characters at font A on 80mm paper
_QR_MAX_BYTES = 700  # stay well under model-2 symbol capacity
_LOGO_WIDTH = 96
_LOGO_HEIGHT = 96


def _logo_pixel(x: int, y: int, width: int, height: int, scale: float = 0.76) -> bool:
    """Return whether a printer pixel belongs to the approved mono mark.

    ESC/POS printers cannot consume SVG. The coordinates below are the
    monochrome mark's viewBox geometry (100x100), transformed from the
    committed ``brand/pharmaos-mark-mono.svg``. Keeping this tiny renderer in
    the API avoids a Pillow/native-image dependency in the offline desktop
    runtime while producing the same mark on every supported printer.
    """
    # Undo the SVG's -45 degree rotation to test the unrotated mark geometry.
    centered_x = (x + 0.5 - width / 2) / scale
    centered_y = (y + 0.5 - height / 2) / scale
    cos_45 = math.sqrt(0.5)
    sin_45 = cos_45
    local_x = centered_x * cos_45 - centered_y * sin_45 + 50
    local_y = centered_x * sin_45 + centered_y * cos_45 + 50

    # Left half of the capsule: a rectangle plus the left semicircle.
    if 27 <= local_x <= 50 and 31 <= local_y <= 69:
        return True
    if local_x <= 27 and (local_x - 27) ** 2 + (local_y - 50) ** 2 <= 19**2:
        return True

    dots = (
        (56, 36, 3.0),
        (56, 50, 3.0),
        (56, 57, 3.0),
        (56, 64, 3.0),
        (63, 36, 3.0),
        (63, 43, 3.0),
        (63, 50, 3.0),
        (63, 64, 3.0),
        (70, 43, 2.8),
        (70, 50, 2.8),
        (70, 57, 2.8),
        (77, 36, 2.6),
        (77, 50, 2.6),
        (77, 57, 2.6),
        (77, 64, 2.6),
        (84, 43, 2.3),
        (84, 50, 2.3),
        (84, 57, 2.3),
    )
    return any((local_x - cx) ** 2 + (local_y - cy) ** 2 <= radius**2 for cx, cy, radius in dots)


def pharmaos_logo_raster(*, width: int = _LOGO_WIDTH, height: int = _LOGO_HEIGHT) -> bytes:
    """Encode the approved PharmaOS mono mark as an ESC/POS raster image."""
    if width < 8 or height < 1:
        raise ValueError("logo raster must be at least 8x1 pixels")
    bytes_per_row = (width + 7) // 8
    bitmap = bytearray(bytes_per_row * height)
    for y in range(height):
        for x in range(width):
            if _logo_pixel(x, y, width, height):
                bitmap[y * bytes_per_row + x // 8] |= 0x80 >> (x % 8)

    header = bytes(
        (
            0,  # m: normal density
            bytes_per_row & 0xFF,
            (bytes_per_row >> 8) & 0xFF,
            height & 0xFF,
            (height >> 8) & 0xFF,
        )
    )
    return RASTER_IMAGE_PREFIX + header + bytes(bitmap)


def qr_code(content: str, *, module_size: int = 6) -> bytes:
    """ESC/POS QR sequence (GS ( k): model 2, size, EC level M, store, print.

    Content is UTF-8. Used for the invoice QR (settings.show_qr_code); when the
    Egyptian e-receipt integration lands (Phase 2), the ETA receipt UUID becomes
    the content — the byte protocol here stays the same.
    """
    data = content.encode("utf-8")[:_QR_MAX_BYTES]
    size = max(1, min(module_size, 16))
    store_len = len(data) + 3
    out = bytearray()
    out += QR_PREFIX + b"\x04\x00\x31\x41\x32\x00"  # model 2
    out += QR_PREFIX + b"\x03\x00\x31\x43" + bytes([size])  # module size
    out += QR_PREFIX + b"\x03\x00\x31\x45\x31"  # error correction M
    out += QR_PREFIX + bytes([store_len % 256, store_len // 256]) + b"\x31\x50\x30" + data
    out += QR_PREFIX + b"\x03\x00\x31\x51\x30"  # print symbol
    return bytes(out)


@dataclass(frozen=True)
class ReceiptLine:
    name: str
    quantity: Decimal
    unit_name: str
    line_total: Decimal


@dataclass(frozen=True)
class ReceiptData:
    """Everything the printed receipt shows. The M9 fields are optional with
    neutral defaults so the M12 walking-skeleton call sites stay valid."""

    pharmacy_name: str
    branch_name: str
    invoice_number: str
    created_at_display: str
    lines: list[ReceiptLine]
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    currency_symbol: str
    thank_you_message: str
    # P2-M6 — VAT total (0 when exempt / no profile); rendered only when > 0.
    tax: Decimal = Decimal("0")
    # P1-M9 — branch-settings driven header/footer
    address: str | None = None
    phone: str | None = None
    license_number: str | None = None
    tax_registration_no: str | None = None
    payment_method_display: str | None = None
    tendered: Decimal | None = None  # cash received from the customer (M10/M11)
    change_due: Decimal | None = None
    qr_content: str | None = None
    show_signature: bool = False
    extra_lines: list[str] = field(default_factory=list)


def _text(value: str) -> bytes:
    return value.encode("utf-8")


def _amount_row(label: str, amount: Decimal, symbol: str) -> bytes:
    amount_text = f"{amount:.2f} {symbol}"
    padding = max(1, _LINE_WIDTH - len(label) - len(amount_text))
    return _text(label + " " * padding + amount_text) + FEED


def build_receipt(receipt: ReceiptData, *, open_drawer: bool = True) -> bytes:
    """Render the full ESC/POS byte stream for an 80mm receipt.

    open_drawer appends the drawer pulse AFTER the cut — pass False for card
    payments/reprints so the drawer only opens when cash actually changes hands.
    """
    out = bytearray()
    out += INIT

    out += ALIGN_CENTER + pharmaos_logo_raster() + FEED
    out += ALIGN_CENTER + DOUBLE_SIZE + BOLD_ON
    out += _text(receipt.pharmacy_name) + FEED
    out += NORMAL_SIZE + BOLD_OFF
    out += _text(receipt.branch_name) + FEED
    if receipt.address:
        out += _text(receipt.address) + FEED
    if receipt.phone:
        out += _text(f"هاتف: {receipt.phone}") + FEED
    if receipt.license_number:
        out += _text(f"ترخيص: {receipt.license_number}") + FEED
    if receipt.tax_registration_no:
        out += _text(f"رقم التسجيل الضريبي: {receipt.tax_registration_no}") + FEED
    out += _text("-" * _LINE_WIDTH) + FEED

    out += ALIGN_RIGHT
    out += _text(f"فاتورة: {receipt.invoice_number}") + FEED
    out += _text(receipt.created_at_display) + FEED
    out += _text("-" * _LINE_WIDTH) + FEED

    out += ALIGN_RIGHT
    for line in receipt.lines:
        out += _text(line.name) + FEED
        qty = f"{line.quantity.normalize()} × {line.unit_name}"
        out += _amount_row(qty, line.line_total, receipt.currency_symbol)
    out += _text("-" * _LINE_WIDTH) + FEED

    out += _amount_row("الإجمالي الفرعي", receipt.subtotal, receipt.currency_symbol)
    if receipt.discount:
        out += _amount_row("الخصم", receipt.discount, receipt.currency_symbol)
    if receipt.tax:
        out += _amount_row("ض.ق.م", receipt.tax, receipt.currency_symbol)
    out += BOLD_ON + DOUBLE_SIZE
    out += _amount_row("الإجمالي", receipt.total, receipt.currency_symbol)
    out += NORMAL_SIZE + BOLD_OFF
    if receipt.payment_method_display:
        out += _text(f"طريقة الدفع: {receipt.payment_method_display}") + FEED
    if receipt.tendered is not None:
        out += _amount_row("المدفوع", receipt.tendered, receipt.currency_symbol)
    if receipt.change_due is not None:
        out += _amount_row("الباقي", receipt.change_due, receipt.currency_symbol)

    if receipt.qr_content:
        out += ALIGN_CENTER + FEED
        out += qr_code(receipt.qr_content)
        out += FEED

    if receipt.show_signature:
        out += ALIGN_CENTER + FEED * 2
        out += _text("." * 24) + FEED
        out += _text("توقيع الصيدلاني") + FEED

    out += ALIGN_CENTER + FEED
    out += _text(receipt.thank_you_message) + FEED
    for extra in receipt.extra_lines:
        out += _text(extra) + FEED
    out += FEED * 3
    out += CUT
    if open_drawer:
        out += DRAWER_PULSE
    return bytes(out)


def send_raw(payload: bytes, *, host: str, port: int = 9100, timeout: float = 5.0) -> None:
    """Send raw bytes to a network ESC/POS printer (JetDirect port 9100).

    USB transport is owned by the Electron main process in production
    (Phase 1); network printing works from any process.
    """
    with socket.create_connection((host, port), timeout=timeout) as conn:
        conn.sendall(payload)
