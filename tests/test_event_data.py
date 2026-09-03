"""
Offline tests for the EventData model and its iCalendar serialization.

None of these need a server or credentials, so they run in a bare checkout. The
server-backed tests live in test_cal_api.py.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from caldav_cal_api.utils.data import EventData, XProperties

PARIS = ZoneInfo("Europe/Paris")


# A realistic server payload: it carries several properties the library deliberately
# does not model, which the retention tests below rely on.
RICH_VEVENT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Some Other Client//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:rich-event-1\r\n"
    "DTSTAMP:20260101T120000Z\r\n"
    "DTSTART;TZID=Europe/Paris:20260114T090000\r\n"
    "DTEND;TZID=Europe/Paris:20260114T100000\r\n"
    "SUMMARY:Weekly sync\r\n"
    "DESCRIPTION:Agenda\\, notes and\\nother things\r\n"
    "LOCATION:Room 3\r\n"
    "STATUS:CONFIRMED\r\n"
    "CATEGORIES:work,meeting\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=WE\r\n"
    "EXDATE;TZID=Europe/Paris:20260121T090000\r\n"
    "TRANSP:OPAQUE\r\n"
    "CLASS:PRIVATE\r\n"
    "ORGANIZER;CN=Alice:mailto:alice@example.com\r\n"
    "ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com\r\n"
    "X-CUSTOM-FLAG:yes\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Reminder\r\n"
    "TRIGGER:-PT15M\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parses_core_fields():
    """Every modeled property is read off a realistic VEVENT."""
    event = EventData.from_ical(RICH_VEVENT, calendar_uid="cal-1")

    assert event.uid == "rich-event-1"
    assert event.summary == "Weekly sync"
    assert event.location == "Room 3"
    assert event.status == "CONFIRMED"
    assert event.calendar_uid == "cal-1"
    assert event.categories == ["work", "meeting"]
    assert event.rrule == "FREQ=WEEKLY;BYDAY=WE"
    assert event.exdate == ["EXDATE;TZID=Europe/Paris:20260121T090000"]
    assert event.description == "Agenda, notes and\nother things"
    assert "X-CUSTOM-FLAG" in event.x_properties
    assert event.x_properties.custom_flag == "yes"
    # synced is a statement about the server, so parsing alone must never assert it.
    assert event.synced is False


def test_timezone_is_normalized_to_utc_but_tzid_is_kept():
    """A TZID event is stored as UTC yet renders back to its original wall clock."""
    event = EventData.from_ical(RICH_VEVENT)

    assert event.dtstart == datetime.datetime(2026, 1, 14, 8, 0, tzinfo=datetime.timezone.utc)
    assert event.dtstart_tzid == "Europe/Paris"
    assert event.dtstart_local.hour == 9
    assert event.dtstart_local.tzinfo == PARIS


def test_roundtrip_is_stable_for_a_parsed_event():
    """to_ical -> from_ical -> to_ical is byte-identical."""
    first = EventData.from_ical(RICH_VEVENT).to_ical()
    second = EventData.from_ical(first).to_ical()
    assert first == second


def test_roundtrip_is_stable_for_a_handbuilt_event():
    """An object with no retained source component serializes stably too."""
    event = EventData(
        summary="Hand built",
        description="Multi\nline, with comma",
        dtstart=datetime.datetime(2026, 5, 1, 14, 30, tzinfo=PARIS),
        dtend=datetime.datetime(2026, 5, 1, 15, 30, tzinfo=PARIS),
        categories=["a", "b"],
        location="Somewhere",
        status="TENTATIVE",
        x_properties={"X-MY-FLAG": "1"},
    )
    first = event.to_ical()
    second = EventData.from_ical(first).to_ical()
    assert first == second


def test_unmodeled_properties_survive_an_update():
    """Changing the summary must not strip attendees, organizer or alarms."""
    event = EventData.from_ical(RICH_VEVENT)
    event.summary = "Renamed sync"
    output = event.to_ical()

    assert "SUMMARY:Renamed sync" in output
    assert "ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com" in output
    assert "ORGANIZER;CN=Alice:mailto:alice@example.com" in output
    assert "BEGIN:VALARM" in output and "TRIGGER:-PT15M" in output
    assert "TRANSP:OPAQUE" in output
    assert "CLASS:PRIVATE" in output
    # And the old summary must be gone, not merely shadowed by a second SUMMARY line.
    assert "Weekly sync" not in output


def test_duration_is_preserved_when_the_event_is_not_retimed():
    """A source using DURATION keeps writing DURATION, and DTEND once retimed."""
    ical = (
        "BEGIN:VEVENT\r\n"
        "UID:dur-1\r\n"
        "DTSTAMP:20260101T120000Z\r\n"
        "DTSTART:20260114T090000Z\r\n"
        "DURATION:PT90M\r\n"
        "END:VEVENT\r\n"
    )
    event = EventData.from_ical(ical)
    assert event.dtend == datetime.datetime(2026, 1, 14, 10, 30, tzinfo=datetime.timezone.utc)

    first = event.to_ical()
    assert "DURATION:PT1H30M" in first
    assert "DTEND" not in first
    assert first == EventData.from_ical(first).to_ical()

    # Retiming invalidates the stored duration, so DTEND takes over.
    event.dtend = datetime.datetime(2026, 1, 14, 12, 0, tzinfo=datetime.timezone.utc)
    retimed = event.to_ical()
    assert "DTEND:20260114T120000Z" in retimed
    assert "DURATION" not in retimed


def test_all_day_event_uses_dates_and_exclusive_dtend():
    """All-day events stay date-valued and keep DTEND exclusive."""
    event = EventData(
        summary="Conference",
        dtstart=datetime.date(2026, 3, 4),
        dtend=datetime.date(2026, 3, 7),
    )
    assert event.all_day is True
    assert event.last_day == datetime.date(2026, 3, 6)
    assert event.duration == datetime.timedelta(days=3)

    output = event.to_ical()
    assert "DTSTART;VALUE=DATE:20260304" in output
    assert "DTEND;VALUE=DATE:20260307" in output

    reparsed = EventData.from_ical(output)
    assert reparsed.all_day is True
    assert reparsed.dtstart == datetime.date(2026, 3, 4)
    assert reparsed.dtend == datetime.date(2026, 3, 7)


def test_all_day_flag_is_reconciled_with_value_types():
    """The all_day flag mirrors dtstart's type in both directions."""
    promoted = EventData(dtstart=datetime.date(2026, 3, 4))
    assert promoted.all_day is True

    demoted = EventData(
        all_day=True,
        dtstart=datetime.datetime(2026, 3, 4, 9, 0, tzinfo=PARIS),
        dtend=datetime.datetime(2026, 3, 5, 9, 0, tzinfo=PARIS),
    )
    assert demoted.all_day is True
    assert demoted.dtstart == datetime.date(2026, 3, 4)
    assert demoted.dtstart_tzid == ""  # DATE values carry no TZID.


def test_zero_length_all_day_event_is_rejected():
    """dtend == dtstart is an error for an all-day event, not a zero-length day."""
    with pytest.raises(ValueError, match="strictly after"):
        EventData(dtstart=datetime.date(2026, 3, 4), dtend=datetime.date(2026, 3, 4))


def test_effective_dtend_applies_rfc_defaults():
    """A missing DTEND reads as one day for DATE starts, zero for DATE-TIME starts."""
    all_day = EventData(dtstart=datetime.date(2026, 3, 4))
    assert all_day.effective_dtend == datetime.date(2026, 3, 5)
    assert all_day.dtend is None  # Faithful to the wire format.

    timed = EventData(dtstart=datetime.datetime(2026, 3, 4, 9, 0, tzinfo=PARIS))
    assert timed.effective_dtend == timed.dtstart
    assert timed.duration == datetime.timedelta(0)


def test_unknown_tzid_falls_back_to_utc_without_raising():
    """One exotic zone must not abort a whole calendar load."""
    event = EventData(
        dtstart=datetime.datetime(2026, 3, 4, 9, 0), dtstart_tzid="Mars/Olympus"
    )
    assert event.dtstart_tzid == ""
    assert event.dtstart.tzinfo == datetime.timezone.utc


def test_recurrence_id_overrides_are_dropped_with_the_master_kept():
    """When a payload holds a master plus overrides, the master wins."""
    payload = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:rec-1\r\nDTSTAMP:20260101T120000Z\r\n"
        "DTSTART:20260114T090000Z\r\nSUMMARY:Master\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:rec-1\r\nDTSTAMP:20260101T120000Z\r\n"
        "RECURRENCE-ID:20260115T090000Z\r\nDTSTART:20260115T110000Z\r\n"
        "SUMMARY:Moved instance\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    event = EventData.from_ical(payload)
    assert event.summary == "Master"
    assert event.rrule == "FREQ=DAILY"


def test_uid_is_minted_for_new_events_but_never_for_parsed_ones():
    """A parsed component with no UID must not be silently forked into a new event."""
    assert EventData(summary="new").uid  # Minted.

    parsed = EventData.from_ical(
        "BEGIN:VEVENT\r\nDTSTAMP:20260101T120000Z\r\nSUMMARY:no uid\r\nEND:VEVENT\r\n"
    )
    assert parsed.uid == ""


def test_to_vcalendar_emits_the_required_vtimezone():
    """A TZID reference without its VTIMEZONE is invalid and some servers reject it."""
    event = EventData(
        summary="Zoned",
        dtstart=datetime.datetime(2026, 1, 14, 9, 0, tzinfo=PARIS),
        dtend=datetime.datetime(2026, 1, 14, 10, 0, tzinfo=PARIS),
    )
    output = event.to_vcalendar()
    assert "BEGIN:VCALENDAR" in output
    assert "BEGIN:VTIMEZONE" in output
    assert "TZID:Europe/Paris" in output
    assert "DTSTART;TZID=Europe/Paris:20260114T090000" in output


def test_to_dict_is_json_serializable():
    """to_dict must emit ISO strings and omit private state."""
    import json

    event = EventData.from_ical(RICH_VEVENT, calendar_uid="cal-1")
    payload = event.to_dict()
    json.dumps(payload)  # Must not raise.

    assert payload["dtstart"] == "2026-01-14T08:00:00+00:00"
    assert payload["dtstart_tzid"] == "Europe/Paris"
    assert "_raw_component" not in payload
    assert payload["x_properties"] == {"X-CUSTOM-FLAG": "yes"}


def test_xproperties_lookup_is_forgiving():
    """Ported XProperties behavior: normalized attributes and case-insensitive keys."""
    props = XProperties({"X-APPLE-SORT-ORDER": "7"})
    assert props.apple_sort_order == "7"
    assert "x-apple-sort-order" in props
    assert props["X-APPLE-SORT-ORDER"] == "7"
    with pytest.raises(AttributeError):
        props.nonexistent
