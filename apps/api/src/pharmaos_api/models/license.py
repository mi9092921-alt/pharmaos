"""license_state — the device-global licensing singleton (P4 §5, migration …2900).

Mirror of the SQL schema; the migrations remain the single source of truth.
The singleton is enforced by the DB (`singleton_key` BOOLEAN CHECK TRUE +
UNIQUE) — a second row is impossible, not just discouraged.

`license_clock_events` deliberately has NO model class: it is append-only and
is touched exclusively through the raw-SQL append/verify protocol in
pharmaos_api.licensing.chain (an ORM would invite updates the DB triggers
forbid).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class LicenseState(Base, MandatoryColumnsMixin):
    __tablename__ = "license_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unlicensed','active','grace','read_only','clock_error',"
            "'key_lost','error','tamper')",
            name="chk_license_state_status",
        ),
    )

    singleton_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), unique=True
    )
    license_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hwid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kid: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    signature: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'unlicensed'")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    high_water_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    anomaly_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tamper_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    verified_from_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_activation_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_activation_license_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_sync_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
