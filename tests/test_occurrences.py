"""
Offline tests for recurrence expansion (EventData.get_occurrences).

The DST test is the important one: it is the whole reason the original TZID is kept
alongside the UTC-normalized datetime.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from caldav_cal_api.utils.data import EventData

PARIS = ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc


def _weekly_paris_event(**overrides) -> EventData:
    """A Wednesday 09:00 Europe/Paris event recurring weekly from 2026-03-11.

    Europe/Paris switches to summer time on 2026-03-29, so a window running into April
    spans the transition.
    """
    kwargs = dict(
        summary="Weekly sync",
        dtstart=datetime.datetime(2026, 3, 11, 9, 0, tzinfo=PARIS),
        dtend=datetime.datetime(2026, 3, 11, 10, 0, tzinfo=PARIS),
        rrule="FREQ=WEEKLY;BYDAY=WE",
    )
    kwargs.update(overrides)
    return EventData(**kwargs)


def test_weekly_expansion_keeps_local_wall_clock_across_dst():
    """Every instance must stay at 09:00 Paris time, before and after the DST switch."""
    event = _weekly_paris_event()
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 15, tzinfo=UTC),
    )

    # 11, 18, 25 March then 1, 8 April. The 15 April instance starts at 07:00 UTC,
    # which is past the window end of 15 April 00:00 UTC, so it is correctly excluded.
    assert len(occurrences) == 5
    local_hours = {occ.dtstart.astimezone(PARIS).hour for occ in occurrences}
    assert local_hours == {9}

    # And the underlying UTC instants genuinely shift by an hour at the transition,
    # which is exactly what expanding over UTC would have got wrong.
    utc_hours = [occ.dtstart.hour for occ in occurrences]
    assert utc_hours == [8, 8, 8, 7, 7]


def test_exdate_removes_an_instance():
    """An EXDATE line drops its occurrence from the expansion."""
    event = _weekly_paris_event(
        exdate=["EXDATE;TZID=Europe/Paris:20260318T090000"],
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
    )
    starts = [occ.dtstart.astimezone(PARIS).date() for occ in occurrences]
    assert datetime.date(2026, 3, 18) not in starts
    assert starts == [datetime.date(2026, 3, 11), datetime.date(2026, 3, 25)]


def test_rdate_adds_an_instance():
    """An RDATE line adds an occurrence the rule would not have produced."""
    event = _weekly_paris_event(
        rdate=["RDATE;TZID=Europe/Paris:20260313T090000"],  # A Friday.
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 3, 20, tzinfo=UTC),
    )
    starts = [occ.dtstart.astimezone(PARIS).date() for occ in occurrences]
    assert datetime.date(2026, 3, 13) in starts


def test_occurrences_carry_the_parent_duration():
    """Each instance keeps the master's length."""
    event = _weekly_paris_event()
    occurrence = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 3, 15, tzinfo=UTC),
    )[0]
    assert occurrence.dtend - occurrence.dtstart == datetime.timedelta(hours=1)
    assert occurrence.event is event
    assert occurrence.summary == "Weekly sync"


def test_all_day_recurrence_yields_dates():
    """All-day recurrence expands to date objects with an exclusive end."""
    event = EventData(
        summary="Daily standup day",
        dtstart=datetime.date(2026, 3, 2),
        dtend=datetime.date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=3",
    )
    occurrences = event.get_occurrences(
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 10)
    )
    assert len(occurrences) == 3
    assert all(occ.all_day for occ in occurrences)
    assert [occ.dtstart for occ in occurrences] == [
        datetime.date(2026, 3, 2),
        datetime.date(2026, 3, 3),
        datetime.date(2026, 3, 4),
    ]
    assert occurrences[0].dtend == datetime.date(2026, 3, 3)  # Exclusive.


def test_non_recurring_event_yields_at_most_itself():
    """A plain event is one occurrence inside the window and none outside it."""
    event = EventData(
        summary="One off",
        dtstart=datetime.datetime(2026, 3, 11, 9, 0, tzinfo=PARIS),
        dtend=datetime.datetime(2026, 3, 11, 10, 0, tzinfo=PARIS),
    )
    inside = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 3, 20, tzinfo=UTC),
    )
    assert len(inside) == 1

    outside = event.get_occurrences(
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 20, tzinfo=UTC),
    )
    assert outside == []


def test_window_matches_on_overlap_not_only_on_start():
    """An event already in progress at the window's left edge still counts."""
    event = EventData(
        summary="Long meeting",
        dtstart=datetime.datetime(2026, 3, 11, 9, 0, tzinfo=UTC),
        dtend=datetime.datetime(2026, 3, 11, 18, 0, tzinfo=UTC),
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 11, 12, 0, tzinfo=UTC),
        datetime.datetime(2026, 3, 11, 13, 0, tzinfo=UTC),
    )
    assert len(occurrences) == 1


def test_limit_caps_the_result():
    """A runaway rule is bounded rather than allowed to exhaust memory."""
    event = EventData(
        summary="Every minute",
        dtstart=datetime.datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
        dtend=datetime.datetime(2026, 3, 11, 0, 1, tzinfo=UTC),
        rrule="FREQ=MINUTELY",
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 11, tzinfo=UTC),
        datetime.datetime(2026, 3, 12, tzinfo=UTC),
        limit=25,
    )
    assert len(occurrences) == 25


def test_reversed_window_is_rejected():
    """An inverted window is a caller bug, not something to silently return [] for."""
    event = _weekly_paris_event()
    with pytest.raises(ValueError, match="must not be before"):
        event.get_occurrences(
            datetime.datetime(2026, 4, 1, tzinfo=UTC),
            datetime.datetime(2026, 3, 1, tzinfo=UTC),
        )


def test_malformed_rrule_degrades_to_the_master_event():
    """One bad rule must not break a whole calendar view."""
    event = _weekly_paris_event(rrule="FREQ=NONSENSE;BYDAY=??")
    occurrences = event.get_occurrences(
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert len(occurrences) == 1
    assert occurrences[0].dtstart.astimezone(PARIS).date() == datetime.date(2026, 3, 11)


def test_recurrence_survives_a_serialization_roundtrip():
    """Expansion must give the same answer before and after a write/read cycle."""
    event = _weekly_paris_event(exdate=["EXDATE;TZID=Europe/Paris:20260318T090000"])
    window = (
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 15, tzinfo=UTC),
    )
    before = [occ.dtstart for occ in event.get_occurrences(*window)]

    reparsed = EventData.from_ical(event.to_ical())
    after = [occ.dtstart for occ in reparsed.get_occurrences(*window)]

    assert before == after
