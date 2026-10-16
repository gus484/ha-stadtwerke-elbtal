"""Statistic-series tests — the rules the recorder is strict about.

A misaligned ``start`` is dropped silently, a decreasing ``sum`` shows up as a
consumption spike, and a ``sum`` that does not start at zero puts the whole
meter reading into the first month. None of that raises; it only looks wrong
in the Energy Dashboard, so it is pinned down here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from api.models import Reading
from series import build_series, reading_start

BERLIN = ZoneInfo("Europe/Berlin")
METER = "1ABC0000000001"


def reading(day: date, value: str) -> Reading:
    return Reading(meter_no=METER, reading_date=day, value=Decimal(value), unit="kWh")


def series(readings, baseline=None):
    return build_series(readings, BERLIN, baseline, label="test")


class TestReadingStart:
    @pytest.mark.parametrize(
        "day,expected",
        [
            # CET, UTC+1
            (date(2025, 1, 31), datetime(2025, 1, 30, 23, tzinfo=UTC)),
            # CEST, UTC+2
            (date(2025, 6, 30), datetime(2025, 6, 29, 22, tzinfo=UTC)),
            # Clocks go forward at 02:00 on 30.03.2025 — midnight is still CET.
            (date(2025, 3, 30), datetime(2025, 3, 29, 23, tzinfo=UTC)),
            (date(2025, 3, 31), datetime(2025, 3, 30, 22, tzinfo=UTC)),
            # Clocks go back at 03:00 on 26.10.2025 — midnight is still CEST.
            (date(2025, 10, 26), datetime(2025, 10, 25, 22, tzinfo=UTC)),
            (date(2025, 10, 27), datetime(2025, 10, 26, 23, tzinfo=UTC)),
        ],
    )
    def test_midnight_berlin_as_utc(self, day: date, expected: datetime) -> None:
        start = reading_start(reading(day, "1"), BERLIN)
        assert start == expected
        assert start.utcoffset().total_seconds() == 0

    def test_back_in_berlin_it_is_the_reading_date(self) -> None:
        # The time-zone check from the TODO: a 31.03. value must not turn
        # into a 30.03. one when viewed in Home Assistant's local time.
        start = reading_start(reading(date(2025, 3, 31), "1"), BERLIN)
        assert start.astimezone(BERLIN).date() == date(2025, 3, 31)


class TestBaseline:
    def test_empty_store_starts_sum_at_zero(self) -> None:
        points = series(
            [reading(date(2025, 1, 31), "10911.1"), reading(date(2025, 2, 28), "11099.5")]
        )
        assert [p.sum for p in points] == [Decimal("0"), Decimal("188.4")]
        assert [p.state for p in points] == [Decimal("10911.1"), Decimal("11099.5")]

    def test_stored_baseline_is_used(self) -> None:
        points = series([reading(date(2025, 2, 28), "11099.5")], Decimal("10911.1"))
        assert points[0].sum == Decimal("188.4")
        assert points[0].state == Decimal("11099.5")

    def test_moving_window_keeps_sums(self, meter_history: list[Reading]) -> None:
        # The portal drops old months over time. A later import that no
        # longer sees the first reading must still write the same sums for
        # the rows both imports share — otherwise it overwrites stored rows
        # with shifted values and the sum jumps at the window edge.
        history = meter_history
        first = series(history)
        baseline = first[0].state - first[0].sum

        oldest_dates = sorted(r.reading_date for r in history)[:3]
        later = series([r for r in history if r.reading_date not in oldest_dates], baseline)

        by_start = {p.start: p.sum for p in first}
        assert later
        assert all(by_start[p.start] == p.sum for p in later)

    def test_readings_below_baseline_are_skipped(self) -> None:
        points = series(
            [reading(date(2024, 12, 31), "10700"), reading(date(2025, 1, 31), "10911.1")],
            Decimal("10911.1"),
        )
        assert [p.sum for p in points] == [Decimal("0")]


class TestMonotonic:
    def test_drop_holds_previous_value(self) -> None:
        points = series(
            [
                reading(date(2025, 1, 31), "100"),
                reading(date(2025, 2, 28), "90"),
                reading(date(2025, 3, 31), "120"),
            ]
        )
        assert [p.sum for p in points] == [Decimal("0"), Decimal("0"), Decimal("20")]

    def test_same_day_last_reading_wins(self) -> None:
        points = series([reading(date(2025, 1, 31), "100"), reading(date(2025, 1, 31), "105")])
        assert len(points) == 1
        assert points[0].state == Decimal("105")


class TestFixtureHistory:
    def test_full_history(self, meter_history: list[Reading]) -> None:
        points = series(meter_history)

        assert len(points) == 18
        starts = [p.start for p in points]
        assert starts == sorted(starts)
        assert all(s.minute == s.second == s.microsecond == 0 for s in starts)
        assert points[0].sum == 0
        sums = [p.sum for p in points]
        assert all(a <= b for a, b in zip(sums, sums[1:]))
        # 2026-01-31 minus 2024-07-31, exactly — no float anywhere yet.
        assert points[-1].sum == Decimal("13364") - Decimal("9695.4")
