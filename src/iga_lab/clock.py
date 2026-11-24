"""Lab clock. Evidence uses this clock so a run is reproducible."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from iga_lab.errors import LoadError

LAB_CLOCK_START = datetime(2026, 2, 20, 15, 0, tzinfo=timezone.utc)


def format_iso(moment: datetime) -> str:
    """Return a UTC timestamp with a Z suffix."""
    if moment.tzinfo is None:
        raise LoadError("Timestamps must include a timezone.")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. A trailing Z is accepted."""
    if not isinstance(value, str) or not value:
        raise LoadError(f"Expected a timestamp string, got {value!r}.")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LoadError(f"Invalid timestamp {value!r}.") from exc
    if moment.tzinfo is None:
        raise LoadError(f"Timestamp {value!r} must include a timezone.")
    return moment.astimezone(timezone.utc)


class ManualClock:
    """A clock that moves only when the lab advances it."""

    def __init__(self, instant: datetime = LAB_CLOCK_START) -> None:
        if instant.tzinfo is None:
            raise LoadError("Clock start must include a timezone.")
        self.instant = instant.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.instant

    def iso(self) -> str:
        return format_iso(self.instant)

    def advance(self, **kwargs: float) -> None:
        self.instant = self.instant + timedelta(**kwargs)
