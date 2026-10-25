"""Daily aggregate observations without request records or business-row locks."""

from datetime import date, datetime
import uuid

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageObservationBucket(Base):
    __tablename__ = "storage_observation_buckets"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # No parent FK: independent observations must not wait on caller row locks.
    location_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    metric: Mapped[str] = mapped_column(String(24), primary_key=True)
    success_count: Mapped[int] = mapped_column(BigInteger)
    failure_count: Mapped[int] = mapped_column(BigInteger)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "metric IN ('original_read','preview_generate')",
            name="ck_storage_observation_metric",
        ),
        CheckConstraint(
            "success_count >= 0 AND failure_count >= 0 AND "
            "(success_count > 0 OR failure_count > 0)",
            name="ck_storage_observation_counts",
        ),
        CheckConstraint(
            "first_observed_at <= last_observed_at",
            name="ck_storage_observation_times",
        ),
    )
