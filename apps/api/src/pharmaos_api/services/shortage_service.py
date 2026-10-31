"""Shortages (النواقص) — pharmacy + market shortage lists.

Pharmacy rows are auto-fed (idempotent sync_auto) from the SAME shared
low-stock threshold the stock list and alerts use; market rows come from the
POS "customer asked" flow and quantify lost demand via ask_count.

Anti-resurrection (owner rule): auto-sync NEVER re-adds an item the staff
closed. Closed auto rows carry suppress_auto_resync (set on manual
cancel/unavailable); it is cleared when stock actually ARRIVES and passes the
threshold (close_on_receive) — i.e. "recovered, then dropped again" re-alerts,
"staff said stop" does not re-alert.

Lifecycle: open → ordered (optionally via a purchase order) → received
(auto-closed by the receiving hook when the threshold is passed) /
unavailable (supplier can't provide; last_checked_at) / cancelled.
"""

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import cast as t_cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import ShortageItem, User
from pharmaos_api.services import audit_service
from pharmaos_api.services.csv_safe import csv_safe
from pharmaos_api.services.inventory_service import LOW_STOCK_THRESHOLD_SQL

KINDS = ("pharmacy", "market")
SOURCES = ("auto_out_of_stock", "auto_low_stock", "manual", "customer_request")
_STATUSES = ("open", "ordered", "received", "unavailable", "cancelled")
_CLOSED = ("received", "unavailable", "cancelled")


# ------------------------------ helpers ------------------------------


def _shortage_dict(s: ShortageItem) -> dict[str, object]:
    return {
        "id": str(s.id),
        "branch_id": str(s.branch_id),
        "kind": s.kind,
        "source": s.source,
        "medication_id": str(s.medication_id) if s.medication_id else None,
        "name_text": s.name_text,
        "qty_needed": str(s.qty_needed),
        "status": s.status,
        "supplier_id": str(s.supplier_id) if s.supplier_id else None,
        "purchase_order_id": str(s.purchase_order_id) if s.purchase_order_id else None,
        "customer_name": s.customer_name,
        "customer_phone": s.customer_phone,
        "ask_count": s.ask_count,
        "last_asked_at": s.last_asked_at.isoformat() if s.last_asked_at else None,
        "ordered_at": s.ordered_at.isoformat() if s.ordered_at else None,
        "received_at": s.received_at.isoformat() if s.received_at else None,
        "last_checked_at": s.last_checked_at.isoformat() if s.last_checked_at else None,
        "suppress_auto_resync": s.suppress_auto_resync,
        "notes": s.notes,
    }


async def _load(
    session: AsyncSession, branch_id: uuid.UUID, shortage_id: uuid.UUID
) -> ShortageItem:
    s = (
        await session.execute(
            select(ShortageItem).where(
                ShortageItem.id == shortage_id,
                ShortageItem.branch_id == branch_id,
                ShortageItem.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise ApiError(ErrorCode.SHORTAGE_INVALID_STATE, 404, message="Shortage item not found.")
    return s


# ------------------------------ reads ------------------------------


async def list_shortages(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    kind: str | None = None,
    status: str | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    where = ["si.branch_id = :b", "NOT si.is_deleted"]
    params: dict[str, object] = {"b": branch_id}
    if kind:
        if kind not in KINDS:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown kind.")
        where.append("si.kind = :k")
        params["k"] = kind
    if status:
        if status not in _STATUSES:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown status.")
        where.append("si.status = :st")
        params["st"] = status
    if search and search.strip():
        where.append("si.name_text ILIKE '%' || :q || '%'")
        params["q"] = search.strip()
    where_sql = " AND ".join(where)
    total = (
        await session.execute(
            text(
                f"SELECT COUNT(*) FROM shortage_items si WHERE {where_sql}"  # noqa: S608 (fragments are constant)
            ).bindparams(**params)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                text(f"""
                SELECT si.id FROM shortage_items si WHERE {where_sql}
                ORDER BY si.updated_at DESC
                OFFSET :skip LIMIT :lim
                """).bindparams(  # noqa: S608 (fragments are constant; values are bound)
                    **params, skip=max(skip, 0), lim=min(max(limit, 1), 200)
                )
            )
        )
        .scalars()
        .all()
    )
    out = []
    for sid in rows:
        item = await session.get(ShortageItem, sid)
        if item is not None:
            out.append(_shortage_dict(item))
    return out, total


# ------------------------------ mutations ------------------------------


async def create_shortage(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    kind: str,
    source: str,
    medication_id: uuid.UUID | None = None,
    name_text: str | None = None,
    qty_needed: Decimal = Decimal(1),
    customer_name: str | None = None,
    customer_phone: str | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    if kind not in KINDS or source not in SOURCES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown kind/source.")
    if qty_needed <= 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Quantity must be positive.")
    if medication_id is None and not (name_text and name_text.strip()):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="A medication or a name is required."
        )
    if medication_id is not None:
        name = (await session.execute(text("""
                    SELECT COALESCE(trade_name_ar, trade_name) FROM medications
                    WHERE id = :m AND NOT is_deleted
                    """).bindparams(m=medication_id))).scalar_one_or_none()
        if name is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown medication.")
        name_text = name
    display_name = (name_text or "").strip()
    if not display_name:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="A medication or a name is required."
        )

    item = ShortageItem(
        branch_id=branch_id,
        kind=kind,
        source=source,
        medication_id=medication_id,
        name_text=display_name,
        qty_needed=qty_needed,
        customer_name=customer_name,
        customer_phone=customer_phone,
        ask_count=1,
        last_asked_at=dt_now() if source == "customer_request" else None,
        notes=notes,
        created_by=actor.id,
    )
    session.add(item)
    try:
        await session.flush()
    except Exception as e:  # the partial unique indexes are the dedupe spine
        raise ApiError(
            ErrorCode.SHORTAGE_DUPLICATE, 422, message="Already on the shortage list."
        ) from e
    await audit_service.record(
        session,
        AuditAction.SHORTAGE_CREATED,
        actor=actor,
        branch_id=branch_id,
        entity_type="shortage_item",
        entity_id=item.id,
        metadata={"kind": kind, "source": source, "name": item.name_text},
    )
    await session.commit()
    return _shortage_dict(item)


def dt_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def patch_shortage(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    shortage_id: uuid.UUID,
    status: str | None = None,
    supplier_id: uuid.UUID | None = None,
    qty_needed: Decimal | None = None,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    """Field updates + status transitions. Manual cancel/unavailable set
    suppress_auto_resync (auto-sync must not resurrect); received clears it."""
    item = await _load(session, branch_id, shortage_id)
    if qty_needed is not None:
        if qty_needed <= 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Quantity must be positive.")
        item.qty_needed = qty_needed
    if supplier_id is not None:
        item.supplier_id = supplier_id
    if customer_name is not None:
        item.customer_name = customer_name
    if customer_phone is not None:
        item.customer_phone = customer_phone
    if notes is not None:
        item.notes = notes

    if status is not None and status != item.status:
        if status not in _STATUSES:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown status.")
        if status == "ordered":
            if item.status != "open":
                raise ApiError(
                    ErrorCode.SHORTAGE_INVALID_STATE,
                    422,
                    message=f"Cannot mark ordered from {item.status}.",
                )
            if item.supplier_id is None:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED, 422, message="A supplier is required to order."
                )
            item.ordered_at = dt_now()
        elif status == "unavailable":
            item.last_checked_at = dt_now()
            item.suppress_auto_resync = True  # staff said stop — do not resurrect
        elif status == "cancelled":
            item.suppress_auto_resync = True
        elif status == "received":
            item.received_at = dt_now()
            item.suppress_auto_resync = False
        item.status = status
    item.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.SHORTAGE_UPDATED,
        actor=actor,
        branch_id=branch_id,
        entity_type="shortage_item",
        entity_id=item.id,
        metadata={"status": item.status, "supplier": str(supplier_id) if supplier_id else None},
    )
    await session.commit()
    return _shortage_dict(item)


async def ask_again(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, shortage_id: uuid.UUID
) -> dict[str, object]:
    """A customer asked again: bump the lost-demand counter; a closed
    'unavailable' item reopens (a human re-confirmed demand — not auto-sync)."""
    item = await _load(session, branch_id, shortage_id)
    item.ask_count += 1
    item.last_asked_at = dt_now()
    if item.status == "unavailable":
        item.status = "open"
    item.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.SHORTAGE_UPDATED,
        actor=actor,
        branch_id=branch_id,
        entity_type="shortage_item",
        entity_id=item.id,
        metadata={"ask_count": item.ask_count, "reopened": item.status == "open"},
    )
    await session.commit()
    return _shortage_dict(item)


# ------------------------------ auto sync ------------------------------


async def sync_auto(session: AsyncSession, *, branch_id: uuid.UUID) -> dict[str, int]:
    """Idempotent upsert of pharmacy shortage rows from the CURRENT tracked
    low/out-of-stock cache state (same shared threshold as the list/alerts).

    Re-add rule (owner): an open row is refreshed; a closed auto row with
    suppress_auto_resync blocks re-adding until stock RECOVERED (the receiving
    hook clears the flag when the threshold is passed)."""
    sql = text(f"""
        WITH candidates AS (
            SELECT bi.medication_id,
                   COALESCE(m.trade_name_ar, m.trade_name) AS name,
                   CASE WHEN bi.cached_quantity = 0 THEN 'auto_out_of_stock'
                        ELSE 'auto_low_stock' END AS src,
                   GREATEST(COALESCE(NULLIF(bi.reorder_point, 0),
                                     NULLIF(bi.min_stock_level, 0), 0)
                            - bi.cached_quantity, 1) AS need
            FROM branch_inventory bi
            JOIN medications m ON m.id = bi.medication_id
            WHERE bi.branch_id = :b AND NOT bi.is_deleted AND NOT m.is_deleted
              AND bi.is_tracked
              AND (bi.cached_quantity = 0
                   OR ({LOW_STOCK_THRESHOLD_SQL} > 0
                       AND bi.cached_quantity <= {LOW_STOCK_THRESHOLD_SQL}))
        )
        INSERT INTO shortage_items (branch_id, kind, source, medication_id, name_text,
                                    qty_needed)
        SELECT :b, 'pharmacy', c.src, c.medication_id, c.name, c.need
        FROM candidates c
        WHERE NOT EXISTS (
            SELECT 1 FROM shortage_items s
            WHERE s.branch_id = :b AND s.medication_id = c.medication_id
              AND s.kind = 'pharmacy' AND s.status IN ('open', 'ordered')
              AND NOT s.is_deleted
        )
          AND NOT EXISTS (
            SELECT 1 FROM shortage_items s
            WHERE s.branch_id = :b AND s.medication_id = c.medication_id
              AND s.kind = 'pharmacy' AND s.source LIKE 'auto\\_%'
              AND s.suppress_auto_resync AND NOT s.is_deleted
          )
        ON CONFLICT (branch_id, medication_id, kind)
            WHERE medication_id IS NOT NULL AND status IN ('open', 'ordered')
              AND NOT is_deleted
        DO UPDATE SET qty_needed = EXCLUDED.qty_needed
        WHERE shortage_items.source LIKE 'auto\\_%'
        """)  # noqa: S608 (fragments are module constants; values are bound)
    result = await session.execute(sql.bindparams(b=branch_id))
    await session.commit()
    return {"rows_affected": t_cast(CursorResult[object], result).rowcount or 0}


async def close_on_receive(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    medication_id: uuid.UUID,
    new_total: Decimal,
) -> int:
    """Receiving hook (called from inventory_service.receive_batch INSIDE the
    caller's transaction — NO commit). When the cached total passes the shared
    threshold (any quantity for untracked items), open/ordered pharmacy rows
    close as received (re-armed), the suppression flags clear (recovered), and
    customer-request rows trigger an in-app notification. Returns the number
    of closed rows."""
    passed = (
        await session.execute(
            text(f"""
                SELECT CASE
                    WHEN {LOW_STOCK_THRESHOLD_SQL} > 0
                        THEN :new_total >= {LOW_STOCK_THRESHOLD_SQL}
                    ELSE :new_total > 0
                END
                FROM branch_inventory bi
                WHERE bi.branch_id = :b AND bi.medication_id = :m AND NOT bi.is_deleted
                """).bindparams(  # noqa: S608 (fragments are module constants; values are bound)
                b=branch_id, m=medication_id, new_total=new_total
            )
        )
    ).scalar_one_or_none()
    if not passed:
        return 0

    closed = (
        t_cast(
            CursorResult[object],
            await session.execute(text("""
                UPDATE shortage_items
                SET status = 'received', received_at = NOW(),
                    suppress_auto_resync = FALSE, updated_at = NOW()
                WHERE branch_id = :b AND medication_id = :m
                  AND kind = 'pharmacy' AND status IN ('open', 'ordered')
                  AND NOT is_deleted
                """).bindparams(b=branch_id, m=medication_id)),
        ).rowcount
        or 0
    )
    # re-arm: recovery happened (threshold passed) — future drops may re-alert.
    # NOT gated on `closed`: a CANCELLED auto row must also re-arm when stock
    # actually arrives — that is exactly the "recovered" signal.
    await session.execute(text("""
            UPDATE shortage_items SET suppress_auto_resync = FALSE, updated_at = NOW()
            WHERE branch_id = :b AND medication_id = :m AND kind = 'pharmacy'
              AND source LIKE 'auto\\_%' AND status IN ('received', 'cancelled', 'unavailable')
              AND NOT is_deleted
            """).bindparams(b=branch_id, m=medication_id))
    if closed == 0:
        return 0
    # in-app "your item arrived" for customer rows (no email — provider gate)
    customers = (await session.execute(text("""
                SELECT id, customer_name FROM shortage_items
                WHERE branch_id = :b AND medication_id = :m
                  AND customer_name IS NOT NULL AND status = 'received'
                  AND NOT is_deleted
                ORDER BY updated_at DESC LIMIT 5
                """).bindparams(b=branch_id, m=medication_id))).all()
    if customers:
        from pharmaos_api.services import notification_service

        for sid, cname in customers:
            await notification_service.notify(
                session,
                branch_id=branch_id,
                channel="in_app",
                priority="medium",
                title_key="shortages.arrived.title",
                body_key="shortages.arrived.body",
                params={"customer": cname, "shortage_id": str(sid)},
            )
    return int(closed)


# ------------------------------ purchase order ------------------------------


async def bulk_transition(
    session: AsyncSession,
    *,
    actor: User,
    ids: list[uuid.UUID],
    action: str,
    supplier_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Bulk status transition over selected rows (mixed branches allowed —
    each row keeps its own branch scope). action: mark_ordered (supplier
    required) | mark_unavailable | cancel | mark_received."""

    if not ids:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="No items selected.")
    if action not in ("mark_ordered", "mark_unavailable", "cancel", "mark_received"):
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown bulk action.")
    done = 0
    for sid in ids:
        item = (
            await session.execute(select(ShortageItem).where(ShortageItem.id == sid))
        ).scalar_one_or_none()
        if item is None or item.is_deleted:
            continue
        if action == "mark_ordered":
            await patch_shortage(
                session,
                actor=actor,
                branch_id=item.branch_id,
                shortage_id=sid,
                status="ordered",
                supplier_id=supplier_id,
            )
        elif action == "mark_unavailable":
            await patch_shortage(
                session,
                actor=actor,
                branch_id=item.branch_id,
                shortage_id=sid,
                status="unavailable",
            )
        elif action == "cancel":
            await patch_shortage(
                session,
                actor=actor,
                branch_id=item.branch_id,
                shortage_id=sid,
                status="cancelled",
            )
        else:
            await patch_shortage(
                session,
                actor=actor,
                branch_id=item.branch_id,
                shortage_id=sid,
                status="received",
            )
        done += 1
    return {"updated": done}


async def create_purchase_order_from_shortages(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    shortage_ids: list[uuid.UUID],
    supplier_id: uuid.UUID,
    notes: str | None = None,
) -> dict[str, object]:
    """Turn selected pharmacy shortage rows into ONE purchase order (draft) via
    purchase_service, then mark them ordered and link the PO id."""
    from pharmaos_api.services import purchase_service
    from pharmaos_api.services.purchase_service import PurchaseLineIn

    if not shortage_ids:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="No items selected.")
    items: list[ShortageItem] = []
    for sid in shortage_ids:
        item = await _load(session, branch_id, sid)
        if item.kind != "pharmacy" or item.medication_id is None:
            raise ApiError(
                ErrorCode.SHORTAGE_INVALID_STATE,
                422,
                message="Only catalog pharmacy items can be ordered.",
            )
        if item.status != "open":
            raise ApiError(
                ErrorCode.SHORTAGE_INVALID_STATE, 422, message=f"{item.name_text}: not open."
            )
        items.append(item)

    lines: list[PurchaseLineIn] = []
    for item in items:
        med_uuid = item.medication_id
        if med_uuid is None:  # filtered above — defensive
            raise ApiError(
                ErrorCode.SHORTAGE_INVALID_STATE,
                422,
                message=f"{item.name_text}: not a catalog item.",
            )
        packaging_id = (await session.execute(text("""
                    SELECT id FROM medication_packaging
                    WHERE medication_id = :m AND NOT is_deleted
                    ORDER BY level DESC LIMIT 1
                    """).bindparams(m=med_uuid))).scalar_one_or_none()
        if packaging_id is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                422,
                message=f"{item.name_text}: no packaging level to order.",
            )
        last_cost = (await session.execute(text("""
                    SELECT purchase_price FROM medication_batches
                    WHERE medication_id = :m AND purchase_price IS NOT NULL
                    ORDER BY received_at DESC LIMIT 1
                    """).bindparams(m=med_uuid))).scalar_one_or_none()
        lines.append(
            PurchaseLineIn(
                medication_id=med_uuid,
                packaging_id=packaging_id,
                qty_ordered=item.qty_needed,
                unit_cost=Decimal(str(last_cost)) if last_cost else Decimal(0),
            )
        )

    po, _ = await purchase_service.create_purchase_order(
        session,
        actor=actor,
        branch_id=branch_id,
        supplier_id=supplier_id,
        lines=lines,
        notes=notes or "أمر شراء من قائمة النواقص",
    )
    now = dt_now()
    for item in items:
        item.status = "ordered"
        item.ordered_at = now
        item.purchase_order_id = po.id
        item.supplier_id = supplier_id
        item.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.SHORTAGE_UPDATED,
        actor=actor,
        branch_id=branch_id,
        entity_type="purchase_order",
        entity_id=po.id,
        metadata={"from_shortages": len(items)},
    )
    await session.commit()
    return {"po_id": str(po.id), "po_number": po.po_number, "items": len(items)}


# ------------------------------ substitutes ------------------------------


async def substitutes(
    session: AsyncSession, *, branch_id: uuid.UUID, shortage_id: uuid.UUID
) -> dict[str, object]:
    """Same ACTIVE ingredient (scientific_name), in stock in this branch.
    NO strength field exists in the catalog — the payload carries the
    pharmacist warning; substitution is always a human decision."""
    item = await _load(session, branch_id, shortage_id)
    if item.medication_id is None:
        return {"warning_key": "shortages.substitute_warning", "items": []}
    rows = (await session.execute(text("""
                SELECT alt.id, COALESCE(alt.trade_name_ar, alt.trade_name) AS name,
                       alt.scientific_name,
                       COALESCE(SUM(b.quantity), 0) AS on_hand
                FROM medications src
                JOIN medications alt
                    ON alt.scientific_name IS NOT NULL
                   AND alt.scientific_name = src.scientific_name
                   AND alt.id <> src.id AND alt.is_active AND NOT alt.is_deleted
                LEFT JOIN medication_batches b
                    ON b.medication_id = alt.id AND b.branch_id = :b
                   AND b.status = 'active' AND NOT b.is_deleted
                WHERE src.id = :m
                GROUP BY alt.id, name, alt.scientific_name
                HAVING COALESCE(SUM(b.quantity), 0) > 0
                ORDER BY on_hand DESC
                """).bindparams(b=branch_id, m=item.medication_id))).all()
    return {
        "warning_key": "shortages.substitute_warning",
        "items": [
            {
                "medication_id": str(r[0]),
                "name": r[1],
                "scientific_name": r[2],
                "on_hand": str(r[3]),
            }
            for r in rows
        ],
    }


# ------------------------------ export ------------------------------


async def export_csv(
    session: AsyncSession, *, branch_id: uuid.UUID, kind: str | None = None
) -> tuple[str, str]:
    where = ["branch_id = :b", "NOT is_deleted"]
    params: dict[str, object] = {"b": branch_id}
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    rows = (
        await session.execute(text(f"""
                SELECT name_text, kind, status, qty_needed, ask_count, customer_name,
                       customer_phone, supplier_id, notes
                FROM shortage_items WHERE {" AND ".join(where)}
                ORDER BY kind, status, updated_at DESC
                """).bindparams(**params))  # noqa: S608 (fragments are constant; values are bound)
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "name",
            "kind",
            "status",
            "qty_needed",
            "ask_count",
            "customer_name",
            "customer_phone",
            "supplier_id",
            "notes",
        ]
    )
    for r in rows:
        writer.writerow(
            [csv_safe(str(c)) if isinstance(c, str) else ("" if c is None else str(c)) for c in r]
        )
    filename = f"shortages-{kind or 'all'}.csv"
    return filename, buf.getvalue()
