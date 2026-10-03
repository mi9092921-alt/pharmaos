"""Expenses + categories (P2-M9).

A SINGLE permission (finance.expenses) gates the whole domain — unlike
prescriptions.*, CLAUDE.md does not split view/create/edit here. Categories
are GLOBAL (shared across branches, like `categories` for medications) and
deactivated rather than deleted; expenses are branch-scoped, full CRUD
(soft-delete), and always derive currency_code from the branch server-side.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Branch, Role, User
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import expense_service


@pytest.fixture
async def actor(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_category(db_session, actor, *, active: bool = True):  # type: ignore[no-untyped-def]
    category = await expense_service.create_category(
        db_session, actor=actor, name_ar=f"فئة {uuid.uuid4().hex[:6]}", name_en="Category"
    )
    if not active:
        await expense_service.update_category(
            db_session, actor=actor, category=category, changes={"is_active": False}
        )
    return category


# ------------------------------ categories ------------------------------


async def test_create_and_list_category(db_session: AsyncSession, actor: User) -> None:
    category = await expense_service.create_category(
        db_session, actor=actor, name_ar="إيجار", name_en="Rent"
    )
    assert category.name_ar == "إيجار" and category.is_active is True

    rows = await expense_service.list_categories(db_session)
    assert any(c["id"] == str(category.id) for c in rows)


async def test_update_category_rename_and_deactivate(db_session: AsyncSession, actor: User) -> None:
    category = await expense_service.create_category(db_session, actor=actor, name_ar="كهرباء")

    updated = await expense_service.update_category(
        db_session, actor=actor, category=category, changes={"name_en": "Electricity"}
    )
    assert updated.name_en == "Electricity" and updated.name_ar == "كهرباء"

    deactivated = await expense_service.update_category(
        db_session, actor=actor, category=category, changes={"is_active": False}
    )
    assert deactivated.is_active is False

    active_rows = await expense_service.list_categories(db_session, active_only=True)
    assert not any(c["id"] == str(category.id) for c in active_rows)
    all_rows = await expense_service.list_categories(db_session, active_only=False)
    assert any(c["id"] == str(category.id) for c in all_rows)


async def test_category_blank_name_rejected(db_session: AsyncSession, actor: User) -> None:
    with pytest.raises(ApiError) as exc:
        await expense_service.create_category(db_session, actor=actor, name_ar="   ")
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


# ------------------------------- expenses -------------------------------


async def test_create_expense_derives_currency_from_branch(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    category = await _make_category(db_session, actor)
    expense = await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=category.id,
        amount=Decimal("250.00"),
        expense_date=dt.date.today(),
        description="فاتورة كهرباء يوليو",
        payment_method="cash",
    )
    assert expense.currency_code == branch.currency_code == "EGP"

    out = await expense_service.get_expense_out(db_session, expense.id)
    assert out["category_name_ar"] == category.name_ar
    assert out["amount"] == "250.00"
    assert out["description"] == "فاتورة كهرباء يوليو"


async def test_expense_rejects_inactive_category(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    inactive = await _make_category(db_session, actor, active=False)
    with pytest.raises(ApiError) as exc:
        await expense_service.create_expense(
            db_session,
            actor=actor,
            branch_id=branch.id,
            expense_category_id=inactive.id,
            amount=Decimal("10"),
            expense_date=dt.date.today(),
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


async def test_expense_rejects_non_positive_amount(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    category = await _make_category(db_session, actor)
    with pytest.raises(ApiError):
        await expense_service.create_expense(
            db_session,
            actor=actor,
            branch_id=branch.id,
            expense_category_id=category.id,
            amount=Decimal("0"),
            expense_date=dt.date.today(),
        )


async def test_expense_rejects_unknown_branch(db_session: AsyncSession, actor: User) -> None:
    category = await _make_category(db_session, actor)
    with pytest.raises(ApiError):
        await expense_service.create_expense(
            db_session,
            actor=actor,
            branch_id=uuid.uuid4(),
            expense_category_id=category.id,
            amount=Decimal("10"),
            expense_date=dt.date.today(),
        )


async def test_update_and_soft_delete_expense(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    category = await _make_category(db_session, actor)
    other_category = await _make_category(db_session, actor)
    expense = await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=category.id,
        amount=Decimal("100.00"),
        expense_date=dt.date.today(),
        payment_method="cash",
    )

    updated = await expense_service.update_expense(
        db_session,
        actor=actor,
        expense=expense,
        changes={
            "amount": Decimal("175.50"),
            "expense_category_id": other_category.id,
            "payment_method": "bank_transfer",
            "description": None,
        },
    )
    assert updated.amount == Decimal("175.50")
    assert updated.expense_category_id == other_category.id
    assert updated.payment_method == "bank_transfer"
    assert updated.description is None

    await expense_service.delete_expense(db_session, actor=actor, expense=expense)
    with pytest.raises(ApiError) as exc:
        await expense_service.get_expense(db_session, expense.id)
    assert exc.value.code == ErrorCode.VALIDATION_FAILED

    rows, total = await expense_service.list_expenses(db_session, branch_id=branch.id)
    assert total == 0 and rows == []


async def test_list_expenses_filters_by_category_and_date_range(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    rent = await _make_category(db_session, actor)
    utilities = await _make_category(db_session, actor)
    today = dt.date.today()

    await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=rent.id,
        amount=Decimal("1000"),
        expense_date=today - dt.timedelta(days=40),
    )
    await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=utilities.id,
        amount=Decimal("300"),
        expense_date=today,
    )

    all_rows, all_total = await expense_service.list_expenses(db_session, branch_id=branch.id)
    assert all_total == 2

    rent_rows, rent_total = await expense_service.list_expenses(
        db_session, branch_id=branch.id, category_id=rent.id
    )
    assert rent_total == 1 and rent_rows[0]["category_name_ar"] == rent.name_ar

    recent_rows, recent_total = await expense_service.list_expenses(
        db_session, branch_id=branch.id, date_from=today - dt.timedelta(days=1)
    )
    assert recent_total == 1 and recent_rows[0]["amount"] == "300.00"


# ------------------------------- API layer -------------------------------


async def _seed_user(db_session: AsyncSession, role_code: str) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"م {role_code}",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    return username


async def _login(client: httpx.AsyncClient, username: str) -> str:
    r = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


async def test_expense_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    category = await _make_category(db_session, actor)
    body = {
        "branch_id": str(branch.id),
        "expense_category_id": str(category.id),
        "amount": "50.00",
        "expense_date": dt.date.today().isoformat(),
        "payment_method": "cash",
    }

    # pharmacist holds prescriptions.* but NOT finance.expenses.
    ph_csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    forbidden = await client.post(
        "/api/v1/finance/expenses", headers={"X-CSRF-Token": ph_csrf}, json=body
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "E-AUTH-002"
    view_forbidden = await client.get(
        "/api/v1/finance/expenses", params={"branch_id": str(branch.id)}
    )
    assert view_forbidden.status_code == 403

    # branch_manager holds finance.expenses; CSRF is mandatory on mutations.
    bm_csrf = await _login(client, await _seed_user(db_session, "branch_manager"))
    no_csrf = await client.post("/api/v1/finance/expenses", json=body)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    created = await client.post(
        "/api/v1/finance/expenses", headers={"X-CSRF-Token": bm_csrf}, json=body
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["amount"] == "50.00" and data["category_name_ar"] == category.name_ar

    listed = await client.get("/api/v1/finance/expenses", params={"branch_id": str(branch.id)})
    assert listed.status_code == 200
    assert any(e["id"] == data["id"] for e in listed.json()["data"])

    deleted = await client.delete(
        f"/api/v1/finance/expenses/{data['id']}", headers={"X-CSRF-Token": bm_csrf}
    )
    assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True


async def test_expense_category_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    body = {"name_ar": "صيانة", "name_en": "Maintenance"}

    await _login(client, await _seed_user(db_session, "cashier"))
    denied = await client.get("/api/v1/finance/expense-categories")
    assert denied.status_code == 403

    su_csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    created = await client.post(
        "/api/v1/finance/expense-categories", headers={"X-CSRF-Token": su_csrf}, json=body
    )
    assert created.status_code == 200, created.text
    cat_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/finance/expense-categories/{cat_id}",
        headers={"X-CSRF-Token": su_csrf},
        json={"is_active": False},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["is_active"] is False
