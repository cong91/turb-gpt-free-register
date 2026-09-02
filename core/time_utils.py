"""Clock helpers for legacy local-wall-clock persistence fields."""
from datetime import datetime, timezone


def local_now() -> datetime:
    """Return local wall-clock time without a timezone marker."""
    return datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None)


def local_today():
    """Return today's date in the local timezone."""
    return local_now().date()


def local_fromtimestamp(value: float) -> datetime:
    """Convert a POSIX timestamp to a local naive datetime."""
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().replace(tzinfo=None)
