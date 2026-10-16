"""Meter readings → cumulative statistic series, without Home Assistant.

The rules the recorder is strict about live here, apart from ``statistics.py``,
so they can be tested without installing Home Assistant.

* ``start`` is midnight of the reading date in the portal's timezone,
  converted to UTC — timezone-aware and hour-aligned.
* ``state`` is the meter reading, ``sum`` counts from a baseline. Home Assistant
  computes the change of the oldest row as ``sum - 0``, so a series whose
  ``sum`` is the raw meter reading shows the whole reading as consumption in
  its first period. Starting ``sum`` at zero avoids that.
* ``sum`` never decreases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api.models import Reading

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One statistic row, still in ``Decimal``."""

    start: datetime
    state: Decimal
    sum: Decimal


def reading_start(reading: Reading, tz: tzinfo) -> datetime:
    """Midnight of the reading date in ``tz``, as UTC."""
    local = datetime.combine(reading.reading_date, time.min, tzinfo=tz)
    return local.astimezone(UTC)


def build_series(
    readings: Iterable[Reading],
    tz: tzinfo,
    baseline: Decimal | None,
    *,
    label: str,
) -> list[SeriesPoint]:
    """Turn readings into hour-aligned, never-decreasing statistic points.

    ``baseline`` is the meter value ``sum`` counts from, taken from what the
    recorder already holds so re-imports line up with earlier ones. ``None``
    means nothing is stored yet: the earliest reading becomes the baseline and
    the series starts at ``sum`` 0.
    """
    # One point per timestamp; a later reading for the same day wins.
    by_start: dict[datetime, Reading] = {}
    for reading in readings:
        by_start[reading_start(reading, tz)] = reading

    points: list[SeriesPoint] = []
    running_max: Decimal | None = None
    for start in sorted(by_start):
        if start.minute or start.second or start.microsecond:
            # Should be unreachable — every European offset is whole hours —
            # but a misaligned row is silently dropped by the recorder, so say
            # so instead of wondering later why a month is missing.
            _LOGGER.warning("%s: dropping reading at %s, not hour-aligned", label, start)
            continue

        value = by_start[start].value
        if running_max is not None and value < running_max:
            # `sum` must never decrease or the Energy Dashboard reads the drop
            # as a meter reset and invents a huge consumption spike.
            _LOGGER.warning(
                "%s: reading at %s is %s, below the previous %s — holding the "
                "previous value so the sum stays monotonic",
                label,
                start,
                value,
                running_max,
            )
            value = running_max
        running_max = value

        if baseline is None:
            baseline = value
        if value < baseline:
            # Older than anything stored. The portal only ever drops old rows,
            # so this should not happen; importing it would put a negative
            # sum in front of the stored history.
            _LOGGER.debug(
                "%s: skipping reading at %s, below the stored baseline %s",
                label,
                start,
                baseline,
            )
            continue

        points.append(SeriesPoint(start=start, state=value, sum=value - baseline))
    return points
