"""branches table mirror."""

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class Branch(MandatoryColumnsMixin, Base):
    __tablename__ = "branches"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
