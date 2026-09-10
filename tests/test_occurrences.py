"""
Offline tests for recurrence expansion (EventData.get_occurrences).

The DST test is the important one: it is the whole reason the original TZID is kept
alongside the UTC-normalized datetime.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from caldav_cal_api.utils.data import EventData, _normalize_rrule_until

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


def test_until_as_a_bare_date_still_expands():
    """A DATE-valued UNTIL beside a timed DTSTART must not kill the expansion.

    RFC 5545 requires UNTIL to be a UTC DATE-TIME when DTSTART is timezone-aware, and
    dateutil enforces it by raising. Real calendars break the rule constantly, and the
    fallback (the master event alone) reads as an event that has stopped repeating. Found
    in a live Nextcloud calendar.
    """
    paris = ZoneInfo("Europe/Paris")
    start = datetime.datetime(2025, 10, 7, 19, 0, tzinfo=paris)
    event = EventData(
        summary="Every four weeks",
        dtstart=start,
        dtend=start + datetime.timedelta(hours=1),
        rrule="FREQ=WEEKLY;UNTIL=20251111;INTERVAL=4;BYDAY=TU",
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2025, 10, 1, tzinfo=datetime.timezone.utc),
        datetime.datetime(2025, 12, 31, tzinfo=datetime.timezone.utc),
    )
    # 7 October and 4 November: the 2 December instance is past UNTIL.
    assert [o.dtstart.astimezone(paris).date() for o in occurrences] == [
        datetime.date(2025, 10, 7),
        datetime.date(2025, 11, 4),
    ]
    # The stored rule is untouched, so what goes back to the server is what came from it.
    assert event.rrule == "FREQ=WEEKLY;UNTIL=20251111;INTERVAL=4;BYDAY=TU"


def test_an_until_on_its_last_day_is_included():
    """ "Through that day" is the reading, so an instance on the UNTIL date survives."""
    paris = ZoneInfo("Europe/Paris")
    start = datetime.datetime(2025, 11, 3, 9, 0, tzinfo=paris)
    event = EventData(
        summary="Daily until",
        dtstart=start,
        dtend=start + datetime.timedelta(minutes=30),
        rrule="FREQ=DAILY;UNTIL=20251105",
    )
    occurrences = event.get_occurrences(
        datetime.datetime(2025, 11, 1, tzinfo=datetime.timezone.utc),
        datetime.datetime(2025, 11, 30, tzinfo=datetime.timezone.utc),
    )
    assert [o.dtstart.astimezone(paris).date() for o in occurrences] == [
        datetime.date(2025, 11, 3),
        datetime.date(2025, 11, 4),
        datetime.date(2025, 11, 5),
    ]


@pytest.mark.parametrize(
    "rrule, expected",
    [
        # A bare DATE becomes the final second of that day, in the expansion zone.
        (
            "FREQ=WEEKLY;UNTIL=20251111;BYDAY=TU",
            "FREQ=WEEKLY;UNTIL=20251111T225959Z;BYDAY=TU",
        ),
        # A naive DATE-TIME is read in the expansion zone.
        ("FREQ=DAILY;UNTIL=20251111T120000", "FREQ=DAILY;UNTIL=20251111T110000Z"),
        # An UNTIL already in UTC is left exactly as it is.
        ("FREQ=DAILY;UNTIL=20251111T120000Z", "FREQ=DAILY;UNTIL=20251111T120000Z"),
        # So is a rule with no UNTIL at all.
        ("FREQ=WEEKLY;COUNT=4;BYDAY=WE", "FREQ=WEEKLY;COUNT=4;BYDAY=WE"),
        # An unreadable value is left alone rather than guessed at.
        ("FREQ=DAILY;UNTIL=next tuesday", "FREQ=DAILY;UNTIL=next tuesday"),
    ],
)
def test_until_normalization(rrule, expected):
    """The rewriting itself, with Europe/Paris as the expansion zone (UTC+1 in November)."""
    assert _normalize_rrule_until(rrule, zone=ZoneInfo("Europe/Paris")) == expected
