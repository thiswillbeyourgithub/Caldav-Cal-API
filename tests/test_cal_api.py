"""
Server-backed tests for CalendarAPI.

Skipped unless the CALDAV_CAL_API_TEST_* variables point at a real server; see
conftest.py. Every test cleans up after itself, but they still need a scratch calendar:
point CALDAV_CAL_API_TEST_CALENDAR_NAME at one you do not mind being written to.
"""

import datetime
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

import pytest

from caldav_cal_api.utils.data import EventData

PARIS = ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc


def _unique_summary(label: str) -> str:
    """A summary no other test or real event can collide with."""
    return f"caldav-cal-api test {label} {uuid.uuid4().hex[:8]}"


def _soon(days: int = 3) -> datetime.datetime:
    """A whole hour a few days from now, well inside the default load window."""
    return (datetime.datetime.now(UTC) + datetime.timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0
    )


def test_calendars_are_fetched(api):
    """The connection yields at least one VEVENT-capable calendar."""
    assert api.calendars, "No calendars loaded."
    for calendar in api.calendars:
        assert calendar.uid
        assert calendar.name


def test_create_update_and_delete_an_event(api, scratch_calendar_uid):
    """The full write lifecycle, verified by reloading between each step."""
    start = _soon()
    event = EventData(
        summary=_unique_summary("lifecycle"),
        description="Created by the test suite.",
        location="Somewhere",
        dtstart=start,
        dtend=start + datetime.timedelta(hours=1),
        calendar_uid=scratch_calendar_uid,
    )

    before = len(api.get_events_by_calendar_uid(scratch_calendar_uid))
    created = api.add_event(event)
    assert created.uid
    assert created.synced is True

    api.load_remote_data()
    assert len(api.get_events_by_calendar_uid(scratch_calendar_uid)) == before + 1

    fetched = api.get_event_by_global_uid(created.uid)
    assert fetched is not None
    assert fetched.summary == event.summary
    assert fetched.location == "Somewhere"

    fetched.summary = _unique_summary("renamed")
    fetched.location = "Elsewhere"
    api.update_event(fetched)

    api.load_remote_data()
    reloaded = api.get_event_by_global_uid(created.uid)
    assert reloaded.summary == fetched.summary
    assert reloaded.location == "Elsewhere"

    assert api.delete_event_by_id(uid=created.uid, calendar_uid=scratch_calendar_uid)
    api.load_remote_data()
    assert api.get_event_by_global_uid(created.uid) is None
    assert len(api.get_events_by_calendar_uid(scratch_calendar_uid)) == before


def test_recurring_event_keeps_its_rrule_through_a_round_trip(
    api, scratch_calendar_uid
):
    """The regression that matters most: expand=False must preserve the RRULE.

    A server-side expanded search would return RECURRENCE-ID instances and lose the rule,
    which would silently turn every recurring event into a single one on the next write.
    """
    start = _soon().astimezone(PARIS)
    event = EventData(
        summary=_unique_summary("recurring"),
        dtstart=start,
        dtend=start + datetime.timedelta(hours=1),
        rrule="FREQ=WEEKLY;COUNT=4",
        calendar_uid=scratch_calendar_uid,
    )
    created = api.add_event(event)

    try:
        api.load_remote_data()
        fetched = api.get_event_by_global_uid(created.uid)
        assert fetched is not None
        assert "FREQ=WEEKLY" in fetched.rrule
        assert fetched.dtstart_tzid == "Europe/Paris"

        occurrences = fetched.get_occurrences(
            datetime.datetime.now(UTC) - datetime.timedelta(days=1),
            datetime.datetime.now(UTC) + datetime.timedelta(days=60),
        )
        assert len(occurrences) == 4
        # Every instance keeps the same local wall-clock time.
        assert len({o.dtstart.astimezone(PARIS).hour for o in occurrences}) == 1

        fetched.summary = _unique_summary("recurring renamed")
        api.update_event(fetched)
        api.load_remote_data()
        after_update = api.get_event_by_global_uid(created.uid)
        assert "FREQ=WEEKLY" in after_update.rrule
    finally:
        api.delete_event_by_id(uid=created.uid, calendar_uid=scratch_calendar_uid)


def test_all_day_event_round_trips_as_dates(api, scratch_calendar_uid):
    """An all-day event stays date-valued with its exclusive DTEND intact."""
    start = (datetime.datetime.now(UTC) + datetime.timedelta(days=5)).date()
    event = EventData(
        summary=_unique_summary("all day"),
        dtstart=start,
        dtend=start + datetime.timedelta(days=2),
        calendar_uid=scratch_calendar_uid,
    )
    created = api.add_event(event)

    try:
        api.load_remote_data()
        fetched = api.get_event_by_global_uid(created.uid)
        assert fetched is not None
        assert fetched.all_day is True
        assert fetched.dtstart == start
        assert fetched.dtend == start + datetime.timedelta(days=2)
        assert fetched.last_day == start + datetime.timedelta(days=1)
    finally:
        api.delete_event_by_id(uid=created.uid, calendar_uid=scratch_calendar_uid)


def test_event_can_delete_itself(api, scratch_calendar_uid):
    """EventData.delete() works on an event carrying an API back-reference."""
    start = _soon(days=4)
    created = api.add_event(
        EventData(
            summary=_unique_summary("self delete"),
            dtstart=start,
            dtend=start + datetime.timedelta(minutes=30),
            calendar_uid=scratch_calendar_uid,
        )
    )
    assert created.delete() is True

    api.load_remote_data()
    assert api.get_event_by_global_uid(created.uid) is None


def test_narrow_window_excludes_far_future_events(api, scratch_calendar_uid):
    """The cache is a window, not the calendar: an out-of-window event is absent."""
    far_start = (datetime.datetime.now(UTC) + datetime.timedelta(days=900)).replace(
        minute=0, second=0, microsecond=0
    )
    created = api.add_event(
        EventData(
            summary=_unique_summary("far future"),
            dtstart=far_start,
            dtend=far_start + datetime.timedelta(hours=1),
            calendar_uid=scratch_calendar_uid,
        )
    )

    try:
        api.load_remote_data(window_start=-1, window_end=30)
        assert api.get_event_by_global_uid(created.uid) is None

        api.load_remote_data(fetch_all=True)
        assert api.get_event_by_global_uid(created.uid) is not None
    finally:
        api.delete_event_by_id(uid=created.uid, calendar_uid=scratch_calendar_uid)
        api.load_remote_data()


def test_unmodeled_properties_survive_a_server_round_trip(api, scratch_calendar_uid):
    """An alarm added out of band must still be there after an update through us."""
    start = _soon(days=6)
    event = EventData(
        summary=_unique_summary("with alarm"),
        dtstart=start,
        dtend=start + datetime.timedelta(hours=1),
        calendar_uid=scratch_calendar_uid,
    )
    # VALARM is deliberately not modeled, so it is injected into the raw component,
    # which is exactly the situation a real client's event arrives in.
    import icalendar

    alarm = icalendar.Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("DESCRIPTION", "Reminder")
    alarm.add("TRIGGER", datetime.timedelta(minutes=-15))
    component = event._build_component()
    component.add_component(alarm)
    event._raw_component = component

    created = api.add_event(event)
    try:
        api.load_remote_data()
        fetched = api.get_event_by_global_uid(created.uid)
        assert "BEGIN:VALARM" in fetched.to_ical()

        fetched.summary = _unique_summary("with alarm renamed")
        api.update_event(fetched)
        api.load_remote_data()
        after = api.get_event_by_global_uid(created.uid)
        assert "BEGIN:VALARM" in after.to_ical()
        assert "TRIGGER" in after.to_ical()
    finally:
        api.delete_event_by_id(uid=created.uid, calendar_uid=scratch_calendar_uid)


def test_read_only_mode_blocks_every_write(read_only_api, scratch_calendar_uid):
    """All three write paths refuse to act when read_only is set."""
    start = _soon(days=8)
    event = EventData(
        summary=_unique_summary("should never exist"),
        dtstart=start,
        dtend=start + datetime.timedelta(hours=1),
        calendar_uid=scratch_calendar_uid,
        uid="read-only-guard-uid",
    )

    with pytest.raises(PermissionError, match="read-only mode"):
        read_only_api.add_event(event)
    with pytest.raises(PermissionError, match="read-only mode"):
        read_only_api.update_event(event)
    with pytest.raises(PermissionError, match="read-only mode"):
        read_only_api.delete_event_by_id(
            uid="read-only-guard-uid", calendar_uid=scratch_calendar_uid
        )


@pytest.mark.parametrize(
    "args",
    [
        ["list-calendars"],
        ["list-upcoming", "--days", "7"],
        ["search", "caldav-cal-api test"],
        ["dump"],
    ],
)
def test_cli_commands_run_successfully(caldav_credentials, test_calendar_name, args):
    """Each CLI command exits cleanly against the real server."""
    command = [
        sys.executable,
        "-m",
        "caldav_cal_api",
        *args,
        "--calendar",
        test_calendar_name,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        pytest.fail(f"CLI command {args} failed: {e.stderr}")
    assert result.returncode == 0
