# Examples

A single script covering the common operations, followed by the runnable scripts that live in the `examples/` directory of the repository.

## Runnable scripts

| Script | What it does |
| --- | --- |
| `weekly_agenda.py` | Print the coming week's agenda, recurrences expanded. |
| `shift_events.py` | Move a selection of events by a fixed offset. |
| `dump_all_calendars_for_git.py` | One `{calendar}.dump` text file per calendar, every event as a VEVENT block, sorted by start time. The calendar counterpart of the sibling project's `dump_all_lists_for_git.py`. |
| `dump_calendars_for_git.py` | The same idea at a finer grain: one `.ics` file per event, under one directory per calendar. |

Both dump scripts exist to be committed on a schedule, so that `git log -p` becomes a history of your calendars. Prefer the per-calendar dump when you want something readable and greppable, and the per-event dump when you want the smallest possible diffs. Neither ever writes to the server: both open the API with `read_only=True`.

Every script in `examples/` carries [PEP 723](https://peps.python.org/pep-0723/) inline metadata, so `uv` installs what it needs on the fly and no virtualenv setup is required:

```bash
uv run examples/weekly_agenda.py --days 7
uv run examples/dump_all_calendars_for_git.py --output-dir ./calendar_dump

# They are executable too, since the shebang hands the file back to uv.
./examples/weekly_agenda.py --days 7
```

Each one calls `load_dotenv()`, so credentials in a `.env` file at the repository root are picked up without exporting anything by hand.

Their output is deterministic, which is what makes the diffs meaningful: dumping an unchanged calendar twice produces identical bytes, so a diff always means something really changed.

```python
import datetime
from zoneinfo import ZoneInfo

from caldav_cal_api import CalendarAPI, EventData

paris = ZoneInfo("Europe/Paris")
utc = datetime.timezone.utc

api = CalendarAPI(
    url="https://your-server.com/",
    username="your-username",
    password="your-password",
    # target_calendars=["Personal"],   # Loading fewer calendars is the biggest speedup
    # window_start=-7, window_end=90,  # Day offsets from now; default -30 / 365
    # fetch_all=True,                  # Ignore the window entirely (can be slow)
    # read_only=True,                  # Dry run: every write raises PermissionError
)

# Nothing is loaded until you ask.
api.load_remote_data()

for calendar in api.calendars:
    print(f"{calendar.name}: {len(calendar.events)} event(s)")
    for event in calendar:
        print(f"  {event.dtstart_local}  {event.summary}")

calendar_uid = api.calendars[0].uid

# --- Create a timed event -------------------------------------------------------
start = datetime.datetime(2026, 5, 1, 14, 30, tzinfo=paris)
event = EventData(
    summary="Coffee with Sam",
    description="Catch up",
    location="The usual place",
    dtstart=start,
    dtend=start + datetime.timedelta(hours=1),
    categories=["personal"],
    calendar_uid=calendar_uid,
)
created = api.add_event(event)
print(created.uid, created.synced)

# --- Create an all-day event ----------------------------------------------------
# DTEND is exclusive, so this covers 4 and 5 March only.
holiday = EventData(
    summary="Long weekend",
    dtstart=datetime.date(2026, 3, 4),
    dtend=datetime.date(2026, 3, 6),
    calendar_uid=calendar_uid,
)
api.add_event(holiday)
print(holiday.all_day, holiday.last_day)  # True, 2026-03-05

# --- Update ---------------------------------------------------------------------
created.summary = "Coffee with Sam (moved)"
created.dtstart = start + datetime.timedelta(days=1)
created.dtend = created.dtstart + datetime.timedelta(hours=1)
api.update_event(created)

# --- Recurrence -----------------------------------------------------------------
weekly = EventData(
    summary="Standup",
    dtstart=datetime.datetime(2026, 3, 11, 9, 0, tzinfo=paris),
    dtend=datetime.datetime(2026, 3, 11, 9, 15, tzinfo=paris),
    rrule="FREQ=WEEKLY;BYDAY=WE",
    exdate=["EXDATE;TZID=Europe/Paris:20260318T090000"],  # Skip one week
    calendar_uid=calendar_uid,
)
api.add_event(weekly)

for occurrence in weekly.get_occurrences(
    datetime.datetime(2026, 3, 1, tzinfo=utc),
    datetime.datetime(2026, 4, 15, tzinfo=utc),
):
    # Always 09:00 local, on both sides of the DST switch.
    print(occurrence.dtstart.astimezone(paris), occurrence.summary)

# --- An agenda across every calendar --------------------------------------------
now = datetime.datetime.now(utc)
for occurrence in api.get_occurrences_in_range(now, now + datetime.timedelta(days=7)):
    print(occurrence)

# --- Delete ---------------------------------------------------------------------
created.delete()                       # Via the object
api.delete_event_by_id(uid=weekly.uid, calendar_uid=calendar_uid)  # Or by UID
```
