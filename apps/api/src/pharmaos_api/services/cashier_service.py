"""Cash sessions (P1-M10): open/close, drawer math, and the Z-report.

Rules:
- ONE open session per cashier per branch (service check + partial unique
  index backstop for races — same pattern as invoice numbering).
- Drawer math (P2-M7 update): expected_cash = opening_float + Σ(payments.amount
  WHERE method='cash' AND cash_session_id=session). payments is the SIGNED
  money ledger (+amount for a sale receipt, -amount for a refund — see
  payment_service/return_service), so this single sum is NET of any cash
  refunds issued during the session — a return's cash outflow reduces the
  drawer exactly like a sale's cash inflow increases it. Sale/refund payment
  rows are distinguished by their FK (invoice_id vs return_id), not by the
  sign of amount, so a future zero-amount sale is still counted correctly.
  Tendered/change cancel out (tendered in, change back out — a cash sale nets
  +total), so they stay invoice-sourced (customer receipt math) and do NOT
  enter the expected-cash formula.
- Close FREEZES expected/counted/discrepancy on the row — a shift's Z numbers
  never drift when later data changes. cash_session.closed is audited in the
  SAME transaction; a nonzero discrepancy is additionally recorded via
  record_independent AFTER commit (audit_service: incident events persist
  regardless of the surrounding operation).
- Day filtering uses the DATABASE's date cast of created_at/opened_at — the
  device runs in the pharmacy's local timezone, matching the local-day
  invoice-numbering convention (INV-YYYYMMDD-…).
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Branch, CashSession, User
from pharmaos_api.services import audit_service

_ZERO = Decimal("0.00")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


async def get_open_session(
    session: AsyncSession, *, branch_id: uuid.UUID, cashier_id: uuid.UUID
) -> CashSession | None:
    """The cashier's open drawer in this branch (used by the sale flow — no commit)."""
    return (
        await session.execute(
            select(CashSession).where(
                CashSession.branch_id == branch_id,
                CashSession.cashier_id == cashier_id,
                CashSession.status == "open",
                CashSession.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()


async def get_session(session: AsyncSession, session_id: uuid.UUID) -> CashSession:
    row = (
        await session.execute(
            select(CashSession).where(
                CashSession.id == session_id, CashSession.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Cash session not found.")
    return row


async def open_session(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, opening_float: Decimal
) -> CashSession:
    if opening_float < 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Negative opening float.")
    branch = await session.get(Branch, branch_id)
    if branch is None or branch.is_deleted or not branch.is_active:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown branch.")
    if await get_open_session(session, branch_id=branch_id, cashier_id=actor.id) is not None:
        raise ApiError(ErrorCode.SESSION_ALREADY_OPEN, 409)

    row = CashSession(
        branch_id=branch_id,
        cashier_id=actor.id,
        opening_float=_q2(opening_float),
        created_by=actor.id,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:  # partial-unique backstop (open race)
        await session.rollback()
        raise ApiError(ErrorCode.SESSION_ALREADY_OPEN, 409) from exc
    await audit_service.record(
        session,
        AuditAction.CASH_SESSION_OPENED,
        actor=actor,
        branch_id=branch_id,
        entity_type="cash_session",
        entity_id=row.id,
        metadata={"opening_float": str(_q2(opening_float))},
    )
    await session.commit()
    await session.refresh(row)
    return row


async def session_summary(session: AsyncSession, cash_session: CashSession) -> dict[str, str | int]:
    """Live drawer summary for one session.

    cash_total/card_total are NET money movement for the session — sourced from
    the payments ledger (P2-M7), so a cash refund issued mid-shift correctly
    reduces expected_cash instead of being invisible to the drawer math (the
    pre-P2-M7 invoice-only sum ignored refunds entirely). tendered/change stay
    invoice-sourced (customer receipt math only).
    """
    pay_rows = (await session.execute(text("""
                SELECT method, COALESCE(SUM(amount), 0) AS net,
                       COUNT(*) FILTER (WHERE invoice_id IS NOT NULL) AS sale_n,
                       COUNT(*) FILTER (WHERE return_id IS NOT NULL) AS refund_n,
                       COALESCE(-SUM(amount) FILTER (WHERE return_id IS NOT NULL), 0) AS refunded
                FROM payments
                WHERE cash_session_id = :s AND NOT is_deleted
                GROUP BY method
                """).bindparams(s=cash_session.id))).all()
    pay = {r[0]: r for r in pay_rows}
    cash_pay = pay.get("cash")
    card_pay = pay.get("card")
    credit_pay = pay.get("store_credit")

    cash_total = Decimal(cash_pay[1]) if cash_pay else _ZERO

    # C5 — cash expenses paid out of THIS drawer reduce expected cash (a cash
    # expense not accounted for would surface as a phantom overage at close).
    exp_row = (await session.execute(text("""
                SELECT COUNT(*), COALESCE(SUM(amount), 0)
                FROM expenses
                WHERE cash_session_id = :s AND NOT is_deleted AND payment_method = 'cash'
                """).bindparams(s=cash_session.id))).one()
    cash_expense_count = int(exp_row[0])
    cash_expenses = Decimal(exp_row[1])
    expected = _q2(cash_session.opening_float + cash_total - cash_expenses)

    tc_row = (await session.execute(text("""
                SELECT COALESCE(SUM(tendered_amount), 0), COALESCE(SUM(change_amount), 0)
                FROM invoices
                WHERE cash_session_id = :s AND NOT is_deleted AND status = 'completed'
                  AND payment_method = 'cash'
                """).bindparams(s=cash_session.id))).one()

    return {
        "cash_count": int(cash_pay[2]) if cash_pay else 0,
        "cash_total": str(_q2(cash_total)),
        "cash_refund_count": int(cash_pay[3]) if cash_pay else 0,
        "cash_refunded": str(_q2(Decimal(cash_pay[4]))) if cash_pay else str(_ZERO),
        "card_count": int(card_pay[2]) if card_pay else 0,
        "card_total": str(_q2(Decimal(card_pay[1]))) if card_pay else str(_ZERO),
        "card_refund_count": int(card_pay[3]) if card_pay else 0,
        "card_refunded": str(_q2(Decimal(card_pay[4]))) if card_pay else str(_ZERO),
        "store_credit_refunded": str(_q2(Decimal(credit_pay[4]))) if credit_pay else str(_ZERO),
        "cash_expense_count": cash_expense_count,
        "cash_expenses": str(_q2(cash_expenses)),
        "tendered_total": str(_q2(Decimal(tc_row[0]))),
        "change_total": str(_q2(Decimal(tc_row[1]))),
        "expected_cash": str(expected),
    }


async def close_session(
    session: AsyncSession,
    *,
    actor: User,
    cash_session: CashSession,
    counted_cash: Decimal,
    notes: str | None = None,
) -> CashSession:
    """Close the drawer: freeze expected/counted/discrepancy + audit.

    cashier.close_session belongs to super_admin/branch_manager (the matrix) —
    the router enforces it; the service accepts any actor so the CLI/tests can
    exercise the flow directly.
    """
    if cash_session.status != "open":
        raise ApiError(ErrorCode.SESSION_NOT_OPEN, 409)
    if counted_cash < 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Negative counted cash.")

    summary = await session_summary(session, cash_session)
    expected = Decimal(str(summary["expected_cash"]))
    counted = _q2(counted_cash)
    discrepancy = _q2(counted - expected)

    cash_session.status = "closed"
    cash_session.closed_at = dt.datetime.now(dt.UTC)
    cash_session.closed_by = actor.id
    cash_session.expected_cash = expected
    cash_session.counted_cash = counted
    cash_session.discrepancy = discrepancy
    cash_session.closing_notes = notes.strip() if notes and notes.strip() else None
    cash_session.updated_by = actor.id

    metadata = {
        "expected_cash": str(expected),
        "counted_cash": str(counted),
        "discrepancy": str(discrepancy),
        "cash_total": str(summary["cash_total"]),
        "card_total": str(summary["card_total"]),
    }
    await audit_service.record(
        session,
        AuditAction.CASH_SESSION_CLOSED,
        actor=actor,
        branch_id=cash_session.branch_id,
        entity_type="cash_session",
        entity_id=cash_session.id,
        metadata=metadata,
    )
    await session.commit()
    await session.refresh(cash_session)

    if discrepancy != 0:
        # Incident event — its own transaction, after the close is durable
        # (audit_service doc: discrepancy must be recorded regardless).
        await audit_service.record_independent(
            AuditAction.CASH_SESSION_DISCREPANCY,
            actor=actor,
            branch_id=cash_session.branch_id,
            entity_type="cash_session",
            entity_id=cash_session.id,
            metadata=metadata,
        )
    return cash_session


async def list_sessions(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    day: dt.date | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    """Sessions for the manager view (cashier name joined), newest first."""
    where = ["cs.branch_id = :b", "NOT cs.is_deleted"]
    params: dict[str, object] = {"b": branch_id}
    if day is not None:
        where.append("CAST(cs.opened_at AS date) = :d")
        params["d"] = day
    if status is not None:
        where.append("cs.status = :st")
        params["st"] = status
    rows = (
        await session.execute(text(f"""
                SELECT cs.id, cs.status, cs.opening_float, cs.opened_at, cs.closed_at,
                       cs.expected_cash, cs.counted_cash, cs.discrepancy, cs.closing_notes,
                       u.username, u.full_name
                FROM cash_sessions cs JOIN users u ON u.id = cs.cashier_id
                WHERE {" AND ".join(where)}
                ORDER BY cs.opened_at DESC
                LIMIT 100
                """).bindparams(**params))  # noqa: S608 (fragments are constant; values are bound)
    ).all()
    return [
        {
            "id": str(r[0]),
            "status": r[1],
            "opening_float": str(r[2]),
            "opened_at": r[3].isoformat(),
            "closed_at": r[4].isoformat() if r[4] else None,
            "expected_cash": str(r[5]) if r[5] is not None else None,
            "counted_cash": str(r[6]) if r[6] is not None else None,
            "discrepancy": str(r[7]) if r[7] is not None else None,
            "closing_notes": r[8],
            "cashier_username": r[9],
            "cashier_full_name": r[10],
        }
        for r in rows
    ]


async def day_report(
    session: AsyncSession, *, branch_id: uuid.UUID, day: dt.date
) -> dict[str, object]:
    """End-of-day Z-report: the branch's local-day GROSS sales by payment
    method, split between in-session sales and sales made outside any drawer
    session (e.g. a pharmacist selling without cashier.open_session) — this
    part is unchanged (still invoice-sourced; existing fields keep their exact
    meaning). P2-M7 adds a refunds breakdown (from the returns ledger, by
    refund_method) and net_total_sales = total_sales - total_refunds, so the
    day view stays consistent with the now refund-aware per-session math
    without changing what the pre-existing fields mean."""
    rows = (await session.execute(text("""
                SELECT payment_method,
                       (cash_session_id IS NOT NULL) AS in_session,
                       COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount
                FROM invoices
                WHERE branch_id = :b AND NOT is_deleted AND status = 'completed'
                  AND CAST(created_at AS date) = :d
                GROUP BY payment_method, (cash_session_id IS NOT NULL)
                """).bindparams(b=branch_id, d=day))).all()

    def _bucket(method: str, in_session: bool) -> tuple[int, Decimal]:
        for r in rows:
            if r[0] == method and bool(r[1]) == in_session:
                return int(r[2]), _q2(Decimal(r[3]))
        return 0, _ZERO

    cash_in_n, cash_in = _bucket("cash", True)
    card_in_n, card_in = _bucket("card", True)
    cash_out_n, cash_out = _bucket("cash", False)
    card_out_n, card_out = _bucket("card", False)
    total_sales = _q2(cash_in + card_in + cash_out + card_out)

    refund_rows = (await session.execute(text("""
                SELECT refund_method, COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount
                FROM returns
                WHERE branch_id = :b AND NOT is_deleted AND CAST(created_at AS date) = :d
                GROUP BY refund_method
                """).bindparams(b=branch_id, d=day))).all()

    def _refund_bucket(method: str) -> tuple[int, Decimal]:
        for r in refund_rows:
            if r[0] == method:
                return int(r[1]), _q2(Decimal(r[2]))
        return 0, _ZERO

    cash_refund_n, cash_refund = _refund_bucket("cash")
    card_refund_n, card_refund = _refund_bucket("card")
    credit_refund_n, credit_refund = _refund_bucket("store_credit")
    total_refunds = _q2(cash_refund + card_refund + credit_refund)

    # C5 — the day's expenses (by payment method, on their own expense_date).
    expense_rows = (await session.execute(text("""
                SELECT payment_method, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount
                FROM expenses
                WHERE branch_id = :b AND NOT is_deleted AND expense_date = :d
                GROUP BY payment_method
                """).bindparams(b=branch_id, d=day))).all()

    def _expense_bucket(method: str) -> tuple[int, Decimal]:
        for r in expense_rows:
            if r[0] == method:
                return int(r[1]), _q2(Decimal(r[2]))
        return 0, _ZERO

    cash_exp_n, cash_exp = _expense_bucket("cash")
    card_exp_n, card_exp = _expense_bucket("card")
    bank_exp_n, bank_exp = _expense_bucket("bank_transfer")
    total_expenses = _q2(cash_exp + card_exp + bank_exp)

    return {
        "date": day.isoformat(),
        "sessions": await list_sessions(session, branch_id=branch_id, day=day),
        "refunds_cash": {"count": cash_refund_n, "total": str(cash_refund)},
        "refunds_card": {"count": card_refund_n, "total": str(card_refund)},
        "refunds_store_credit": {"count": credit_refund_n, "total": str(credit_refund)},
        "total_refunds": str(total_refunds),
        "net_total_sales": str(_q2(total_sales - total_refunds)),
        "expenses_cash": {"count": cash_exp_n, "total": str(cash_exp)},
        "expenses_card": {"count": card_exp_n, "total": str(card_exp)},
        "expenses_bank_transfer": {"count": bank_exp_n, "total": str(bank_exp)},
        "total_expenses": str(total_expenses),
        "cash_in_session": {"count": cash_in_n, "total": str(cash_in)},
        "card_in_session": {"count": card_in_n, "total": str(card_in)},
        "cash_outside_sessions": {"count": cash_out_n, "total": str(cash_out)},
        "card_outside_sessions": {"count": card_out_n, "total": str(card_out)},
        "invoice_count": cash_in_n + card_in_n + cash_out_n + card_out_n,
        "total_sales": str(total_sales),
    }
