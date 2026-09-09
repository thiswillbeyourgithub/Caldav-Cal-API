<!-- TODO: add the PyPI badge once the package is published, and the ReadTheDocs link
     once the docs are built:
     [![PyPI version](https://badge.fury.io/py/caldav-cal-api.svg)](https://badge.fury.io/py/caldav-cal-api)
-->

# CalDAV-Cal-API

> [!WARNING]
> **This project is fully vibecoded.** Every line was written by [Claude Code](https://claude.com/claude-code), from an architecture I specified but did not hand-write, and I have not audited it line by line. Read it before pointing it at a calendar you care about, and keep backups.
>
> It exists because I wanted what my [CalDAV-Tasks-API](https://github.com/thiswillbeyourgithub/CaldavTasksAPI/) gives me for tasks (VTODOs), but for calendar events (VEVENTs). It therefore mimics that project's layout and architecture on purpose, so the two feel like the same library from the outside.

Python library and command-line interface for CalDAV calendars (VEVENTs). Connect to a CalDAV server, read your calendars and events as plain Python objects, and create, modify or delete events without touching iCalendar text by hand.

## Table of Contents

- [Motivation and Purpose](#motivation-and-purpose)
- [Compatibility](#compatibility)
- [Features](#features)
- [Design decisions worth knowing](#design-decisions-worth-knowing)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Python API](#python-api)
  - [Command Line Interface](#command-line-interface)
- [Testing](#testing)
- [Contributing](#contributing)

## Motivation and Purpose

Reading a calendar from Python usually means either driving the `caldav` library directly, which leaves you
parsing iCalendar and juggling timezones, or reaching for something that hides so much you cannot tell what
it sends to the server. This library sits between the two:

1. Calendars and events as ordinary dataclasses you can read, mutate and write back.
2. Timezone handling that is correct by default, including across DST transitions.
3. A small read-only CLI for the things you would otherwise write a throwaway script for.
4. Few dependencies, so it stays easy to drop into a larger project.

## Compatibility

Developed and tested against **Nextcloud**. It should work with any RFC 4791 server; reports from other
servers are welcome. Pass `nextcloud_mode=False` if your server does not use Nextcloud's
`remote.php/dav/` path layout.

## Features

- Read calendars and events into plain Python objects.
- Create, update and delete events.
- Timezone-aware datetimes, normalized to UTC internally while preserving the original `TZID`.
- All-day events as `date` objects, with the exclusive `DTEND` handled correctly.
- Recurrence stored raw (`RRULE` / `RDATE` / `EXDATE`) with an opt-in `get_occurrences()` expander that
  respects DST.
- Properties this library does not model (attendees, organizer, alarms, and so on) are preserved verbatim
  across updates.
- Read-only mode, for dry runs and for code that must not write.
- A read-only CLI: `list-calendars`, `list-upcoming`, `search`, `dump`.
- Configuration via environment variables.

## Design decisions worth knowing

**The cache is a window, not the calendar.** A calendar can hold decades of events, so `load_remote_data()`
fetches a bounded date range: by default the last 30 days and the next year. An event outside that range is
simply not loaded, and `get_event_by_global_uid()` will return `None` for it. Pass `fetch_all=True` (or set
`CALDAV_CAL_API_FETCH_ALL`) when you genuinely need everything.

**Only the master event is modeled.** Recurrence is stored raw and expanded on demand. Per-occurrence
modifications (`RECURRENCE-ID` overrides) are not represented; when one is encountered it is dropped with a
warning rather than silently. There is no "this event / this and future / all events" edit distinction.

**`DTEND` is exclusive.** A single all-day event on 4 March has `dtstart=date(2026, 3, 4)` and
`dtend=date(2026, 3, 5)`. Use the `last_day` property when you want the inclusive final day for display.

**Unmodeled properties survive.** Each event keeps its source component, and writing rewrites only the
properties this library owns. Changing a meeting's summary will not strip its attendees or everyone's
reminders.

## Installation

```bash
uv pip install caldav-cal-api
```

From source:

```bash
git clone <repository_url>
cd caldav_cal_api_repo

uv pip install .          # Runtime only
uv pip install -e .       # Editable
uv pip install -e ".[dev]"  # With test and release tooling
```

Requires Python 3.10 or newer.

## Configuration

Every setting can come from an environment variable; see `.env.example` for a copy-paste starting point.

| Variable | Purpose |
| --- | --- |
| `CALDAV_CAL_API_URL` | CalDAV server URL |
| `CALDAV_CAL_API_USERNAME` | CalDAV username |
| `CALDAV_CAL_API_PASSWORD` | CalDAV password |
| `CALDAV_CAL_API_DEFAULT_CALENDAR_UID` | Calendar used when a call does not name one |
| `CALDAV_CAL_API_WINDOW_START_DAYS` | Load window start, in days from now (default `-30`) |
| `CALDAV_CAL_API_WINDOW_END_DAYS` | Load window end, in days from now (default `365`) |
| `CALDAV_CAL_API_FETCH_ALL` | `1`/`true`/`yes`/`on` to ignore the window and load everything |
| `CALDAV_CAL_API_LOG_LEVEL` | Console log level (default `INFO`; the log file is always `DEBUG`) |
| `CALDAV_CAL_API_TEST_URL` | Test server URL (server-backed tests only) |
| `CALDAV_CAL_API_TEST_USERNAME` | Test username |
| `CALDAV_CAL_API_TEST_PASSWORD` | Test password |
| `CALDAV_CAL_API_TEST_CALENDAR_NAME` | Scratch calendar the write tests may modify |

## Usage

### Python API

```python
import datetime
from zoneinfo import ZoneInfo

from caldav_cal_api import CalendarAPI, EventData

api = CalendarAPI(
    url="https://your-server.com/",
    username="your-username",
    password="your-password",
    # target_calendars=["Personal"],  # Loading fewer calendars is the biggest speedup
    # window_start=-7, window_end=90,  # Day offsets from now; default -30 / 365
    # fetch_all=True,                  # Ignore the window entirely (can be slow)
    # read_only=True,                  # Refuse every write
)

api.load_remote_data()

for calendar in api.calendars:
    print(f"{calendar.name}: {len(calendar.events)} event(s)")
    for event in calendar:
        print(f"  {event.dtstart_local}  {event.summary}")

# Create an event
paris = ZoneInfo("Europe/Paris")
start = datetime.datetime(2026, 5, 1, 14, 30, tzinfo=paris)
event = EventData(
    summary="Coffee with Sam",
    location="The usual place",
    dtstart=start,
    dtend=start + datetime.timedelta(hours=1),
    calendar_uid=api.calendars[0].uid,
)
created = api.add_event(event)

# Update it
created.summary = "Coffee with Sam (moved)"
api.update_event(created)

# An all-day event: DTEND is exclusive, so this covers 4 and 5 March only
holiday = EventData(
    summary="Long weekend",
    dtstart=datetime.date(2026, 3, 4),
    dtend=datetime.date(2026, 3, 6),
    calendar_uid=api.calendars[0].uid,
)
api.add_event(holiday)
print(holiday.all_day, holiday.last_day)  # True, 2026-03-05

# Expand a recurring event, correctly across DST
weekly = EventData(
    summary="Standup",
    dtstart=datetime.datetime(2026, 3, 11, 9, 0, tzinfo=paris),
    dtend=datetime.datetime(2026, 3, 11, 9, 15, tzinfo=paris),
    rrule="FREQ=WEEKLY;BYDAY=WE",
    calendar_uid=api.calendars[0].uid,
)
for occurrence in weekly.get_occurrences(
    datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc),
    datetime.datetime(2026, 4, 15, tzinfo=datetime.timezone.utc),
):
    print(occurrence.dtstart.astimezone(paris))  # Always 09:00 local

# Delete it
created.delete()
```

### Command Line Interface

All CLI commands are read-only.

```bash
# List the calendars on the server (JSON)
caldav-cal-api list-calendars

# The next week's agenda, recurring events expanded
caldav-cal-api list-upcoming --days 7

# Restrict to one calendar and emit JSON
caldav-cal-api list-upcoming --calendar Personal --json

# Find events mentioning "dentist" in a given range
caldav-cal-api search dentist --start 2026-01-01 --end 2026-12-31

# Dump raw VEVENTs, e.g. to diff a calendar into version control
caldav-cal-api dump --calendar Personal
```

Every command also works as `python -m caldav_cal_api <command>`, and supports `--help`, `--debug`
(verbose logging plus an interactive console with `api` in scope), and the shared connection options.

## Testing

```bash
uv pip install -e ".[dev]"
pytest
```

The offline tests (data model, iCalendar serialization, recurrence expansion) run in a bare checkout. The
server-backed tests are skipped unless the four `CALDAV_CAL_API_TEST_*` variables are set; point
`CALDAV_CAL_API_TEST_CALENDAR_NAME` at a scratch calendar, since those tests create and delete events in
it.

## Contributing

Issues and pull requests are welcome. Please keep to the existing style (black, NumPy-style docstrings) and
add a test for any bug you fix.

This project was written with the help of [Claude Code](https://claude.com/claude-code).
