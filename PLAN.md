# CaldavCalAPI - implementation plan

Plan written with Claude Code. Keep this file updated as work progresses so a future session can pick up where the last one stopped.

**Status: complete.** Every planned step is done and no placeholders remain; see Open TODOs at the bottom.

## Context

`caldav_tasks_api` (sibling repo at `../caldav_tasks_api_repo`, v1.8.0, AGPLv3, same author) is a CalDAV client for **VTODO** tasks: plain Python objects, an in-memory cache, a click CLI, sphinx/RTD docs. It works well and its layout is worth reusing.

**CaldavCalAPI** is the same architecture for **VEVENT** calendars, so calendars can be read, created and modified through simple Python objects.

## Progress checklist

- [x] 1. `PLAN.md`, `.gitignore`, `.pre-commit-config.yaml`, initial commit
- [x] 2. `setup.py`, `bumpver.toml`, package skeleton, `utils/logging_config.py`
- [x] 3. `utils/data.py`: `XProperties`, `CalendarData`, `EventData` fields + `__post_init__` invariant
- [x] 4. `EventData.from_ical` / `to_ical` / `to_vcalendar` / `to_dict` + offline round-trip tests (`tests/test_event_data.py`)
- [x] 5. `Occurrence` + `get_occurrences` + offline tests (`tests/test_occurrences.py`)
- [x] 6. `caldav_cal_api.py`: connect, `_adjust_url`, `_fetch_raw_calendars`, `load_remote_data` + window
- [x] 7. `add_event` / `update_event` / `delete_event_by_id` + read-only guards
- [x] 8. `__main__.py`: the four CLI commands
- [x] 9. `tests/conftest.py` + integration tests against a real server (`tests/test_cal_api.py`)
- [x] 10. `README.md`, `.env.example`
- [x] 11. `docs/` sphinx layout + `.readthedocs.yaml`
- [x] 12. `examples/`

`LICENSE` is the sibling's AGPLv3 file, copied verbatim at the user's request. Do not generate a license file.

## Locked decisions

| Topic | Decision |
| --- | --- |
| Component scope | VEVENT only. No VJOURNAL, no free/busy. |
| Field scope | Core fields only. No attendees/organizer/alarms as first-class fields. |
| Datetimes | Timezone-aware, normalized to UTC internally, original TZID preserved so writes round-trip. All-day stays `date` + `all_day` flag. |
| Recurrence | `RRULE`/`RDATE`/`EXDATE` stored raw, plus one opt-in `get_occurrences(start, end)` helper using `dateutil`. No `RECURRENCE-ID` override objects, no "this and future" edit semantics. |
| Serialization | Use the `icalendar` library (declared explicitly), **not** the sibling's hand-rolled string concat. |
| Raw retention | Keep a private `_raw_component`; `to_ical()` rewrites only a managed property set. Diverges from the sibling, see below. |
| Load scope | Date window via caldav search, default -30d/+365d, configurable by ctor args and env vars, with a `fetch_all` escape hatch. |
| CLI | Four read-only commands: `list-calendars`, `list-upcoming`, `search`, `dump`. `search TEXT [--start] [--end]`: range overrides the window server-side, text filters client-side. |
| Docstrings | NumPy style (per the user's dev-pref), unlike the sibling's Google style. |
| Not doing | No `create_calendar`/`delete_calendar`. No write commands in the CLI for v0.1. |

### Why raw retention diverges from the sibling

The sibling can afford to regenerate VTODO from scratch because VTODO carries little beyond what `TaskData` models. VEVENT does not. Having declined to model attendees, organizer and alarms, a non-retaining `to_ical()` would make every `update_event()` a silent data loss: change a summary, strip the meeting's participants and everyone's reminders. `_raw_component` is `repr=False, compare=False`, so `__eq__` still compares modeled fields only.

## Layout

```
caldav_cal_api_repo/
|-- caldav_cal_api/
|   |-- __init__.py            exports CalendarAPI, CalendarData, EventData, Occurrence, VERSION
|   |-- __main__.py            click CLI, entry point `caldav-cal-api`
|   |-- caldav_cal_api.py      CalendarAPI (VERSION lives here, bumpver-tracked)
|   `-- utils/
|       |-- __init__.py        empty
|       |-- data.py            CalendarData / XProperties / EventData / Occurrence
|       `-- logging_config.py  loguru, setup_logging() at import, enable_debug_logging()
|-- tests/ (__init__.py, conftest.py, test_cal_api.py)
|-- docs/  (requirements.txt, source/{conf.py,index.rst,*.rst,the_*.md})
|-- examples/
|-- setup.py, bumpver.toml, README.md, PLAN.md
`-- .pre-commit-config.yaml (black), .readthedocs.yaml, .gitignore
```

`XProperties` and `logging_config.py` are ported near-verbatim from the sibling, with the app name changed to `caldav-cal-api` and the env var to `CALDAV_CAL_API_LOG_LEVEL`.

## Data model (`caldav_cal_api/utils/data.py`)

Stdlib dataclasses, fields alphabetical with an inline comment naming the iCal property (sibling house style).

### `CalendarData`

Analogue of `TaskListData`: `color`, `deleted`, `name`, `synced`, `uid`, `events: list[EventData]`, plus a `__post_init__` uuid4 default, `__str__`/`__repr__`, `__iter__` over events, `to_dict()`.

### `EventData`

```python
@dataclass
class EventData:
    all_day: bool = False                                     # DTSTART;VALUE=DATE (derived from dtstart's type)
    calendar_uid: str = ""                                    # Owning CalendarData.uid (not an iCal property)
    categories: list[str] = field(default_factory=list)       # CATEGORIES
    changed_at: datetime.datetime | None = None               # LAST-MODIFIED (UTC per RFC 5545)
    created_at: datetime.datetime | None = None               # DTSTAMP (UTC per RFC 5545)
    description: str = ""                                     # DESCRIPTION
    dtend: datetime.datetime | datetime.date | None = None    # DTEND (exclusive bound)
    dtend_tzid: str = ""                                      # DTEND;TZID= (IANA name; "" = UTC/floating/all-day)
    dtstart: datetime.datetime | datetime.date | None = None  # DTSTART
    dtstart_tzid: str = ""                                    # DTSTART;TZID=
    exdate: list[str] = field(default_factory=list)           # EXDATE, raw values, one per line
    location: str = ""                                        # LOCATION
    rdate: list[str] = field(default_factory=list)            # RDATE, raw values, one per line
    rrule: str = ""                                           # RRULE, raw value
    sequence: int = 0                                         # SEQUENCE
    status: str = ""                                          # STATUS (TENTATIVE|CONFIRMED|CANCELLED)
    summary: str = ""                                         # SUMMARY
    synced: bool = False                                      # Internal, set by CalendarAPI only
    uid: str = ""                                             # UID
    x_properties: XProperties = field(default_factory=XProperties)
    _api_reference: Optional["CalendarAPI"] = field(default=None, repr=False, compare=False)
    _href: str = field(default="", repr=False, compare=False)  # hrefs are not always "<uid>.ics"
    _raw_component: Optional[icalendar.Event] = field(default=None, repr=False, compare=False)
    _source_duration: Optional[datetime.timedelta] = field(default=None, repr=False, compare=False)
```

**TZID via companion fields, not a wrapper type.** `dtstart` stays a real `datetime`, so comparison, `sorted()`, `dateutil` and caldav search all work unwrapped. A `ZonedDateTime` wrapper would break `==` against plain datetimes and force `.dt` at every call site.

Read-only convenience properties: `dtstart_local`, `dtend_local`, `duration`, `effective_dtend` (RFC 5545 section 3.6.1 defaults when DTEND is absent), `last_day` (all-day inclusive view).

**`all_day` invariant**, enforced coercively in `__post_init__` (never raising, since a dataclass cannot tell "caller passed False" from "default"):

1. `all_day is True` iff `dtstart is not None and not isinstance(dtstart, datetime.datetime)`. Every check uses `isinstance(x, datetime.datetime)`, never `isinstance(x, datetime.date)` (a datetime *is* a date).
2. `all_day=True` + datetime dtstart -> `.date()` both ends, clear both tzids, `logger.debug`. `all_day=False` + date dtstart -> set `all_day = True`, `logger.debug`.
3. All-day implies both tzids forced `""`.
4. Timed implies aware and UTC-normalized. Naive + tzid -> localize via `ZoneInfo`; naive without -> assume UTC with a `logger.warning`. Aware in another zone -> `astimezone(utc)`, capturing the IANA key into the tzid field if empty. An unresolvable TZID falls back to `""` with a warning rather than raising, so one exotic server zone cannot poison a whole load.
5. `dtend > dtstart` when both set, else `ValueError`. **All-day DTEND is exclusive**: a one-day event on 2026-03-04 is `dtstart=date(2026,3,4)`, `dtend=date(2026,3,5)`. `dtend == dtstart` all-day is an error, not a zero-length day; `last_day` exists so callers do not "fix" this themselves.
6. `uid` defaults to `uuid4()` **only when `_raw_component is None`**. Never mint a UID for a server object.
7. `x_properties` given a dict is upgraded to `XProperties`.

**DTEND vs DURATION.** Public surface is DTEND only; `duration` is computed. `from_ical`: DTEND present -> stored as-is; DURATION present -> `dtend = dtstart + duration` with the original kept in `_source_duration`; neither -> `dtend = None`. `to_ical`: if `_source_duration` is set *and* still matches `dtend - dtstart`, write DURATION and no DTEND (preserves nominal duration across DST for recurring events); otherwise write DTEND and delete DURATION.

### Serialization

```python
@staticmethod
def from_ical(ical: str | bytes | icalendar.Event, calendar_uid: str) -> EventData: ...
def to_ical(self) -> str: ...   # bare VEVENT block, mirrors TaskData.to_ical
def to_vcalendar(self, *, prodid: str = "-//caldav_cal_api//EN") -> str: ...
```

- `from_ical` parses via `icalendar.Calendar.from_ical()` (falling back to `icalendar.Event.from_ical()` for a bare component), walks `VEVENT`, and selects the **first component with no RECURRENCE-ID** as the master. Overrides are dropped with a `logger.warning` naming the UID: the visible consequence of the "no override objects" decision. `icalendar` yields `vDDDTypes`, so `prop.dt` gives the value and `prop.params.get("TZID")` the zone, exactly the pair the companion fields need.
- `to_ical()` deep-copies `_raw_component` (or starts a fresh `icalendar.Event()`) and writes only: `UID, SUMMARY, DESCRIPTION, LOCATION, STATUS, CATEGORIES, DTSTART, DTEND, DURATION, RRULE, RDATE, EXDATE, DTSTAMP, LAST-MODIFIED, SEQUENCE` plus every `X-*` (owned by `x_properties`). Everything else (ATTENDEE, ORGANIZER, VALARM, TRANSP, CLASS, GEO, URL, ATTACH, RELATED-TO) is re-emitted verbatim. Both paths funnel through one `_build_component()` writing the managed set in a fixed order, which is what makes `to_ical -> from_ical -> to_ical` stable.
- `to_vcalendar()` is what gets PUT: `DTSTART;TZID=` without a matching VTIMEZONE is invalid per RFC 5545 and some servers reject it. `icalendar >= 6.0` synthesizes one via `icalendar.Timezone.from_tzinfo`.
- `to_dict()` emits ISO 8601 via `.isoformat()` and excludes `_raw_component` / `_api_reference`.
- Output is CRLF (icalendar is RFC-correct), unlike the sibling's `\n`. Tests must assert accordingly.

### `Occurrence` and expansion

```python
@dataclass(frozen=True)
class Occurrence:
    all_day: bool
    dtend: datetime.datetime | datetime.date            # Exclusive, UTC-normalized
    dtstart: datetime.datetime | datetime.date
    event: "EventData" = field(repr=False, compare=False)

def get_occurrences(self, start, end, *, limit: int = 1000) -> list[Occurrence]: ...
```

Returns `Occurrence`, not `EventData` clones: clones would each carry the master's UID, corrupting any uid-keyed cache and inviting `update_event()` on a phantom with no server representation.

- Half-open window `[start, end)`, instances included on **overlap** (`occ_start < end and occ_end > start`). A `date` is coerced to midnight UTC; a naive datetime treated as UTC with a warning. `end < start` raises.
- Feeds `dateutil.rrule.rrulestr("\n".join(lines), dtstart=..., forceset=True)` with the reassembled `RRULE:`/`RDATE:`/`EXDATE:` text. `forceset=True` makes dateutil apply RDATE/EXDATE itself, which is the payoff for storing them raw. Unparseable rules log an error and yield the master only, never raise.
- **Expansion happens in the original TZID zone, then converts back to UTC.** "Every Monday 09:00 Europe/Paris" expanded over UTC instants drifts an hour at each DST transition. This is the main reason `dtstart_tzid` exists.
- Duration held constant at `effective_dtend - dtstart`.
- All-day recurrence: anchor at midnight UTC (DATE values are DST-immune), expand, then `.date()` the results; `dtend` stays an exclusive date.
- `limit` caps at 1000 with a warning, guarding against `FREQ=SECONDLY` over a wide window.

## API (`caldav_cal_api/caldav_cal_api.py`)

```python
class CalendarAPI:
    VERSION: str = "0.1.0"   # bumpver-tracked

    def __init__(
        self,
        url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        nextcloud_mode: bool = True,
        debug: bool = False,
        target_calendars: Optional[list[str]] = None,
        read_only: bool = False,
        ssl_verify_cert: bool = True,
        window_start: int | datetime.datetime | None = None,  # int = days offset from now (negative = past), default -30
        window_end: int | datetime.datetime | None = None,    # default +365
        fetch_all: bool = False,                              # ignore the window, fetch every VEVENT
    ) -> None: ...

    def load_remote_data(self, *, window_start=None, window_end=None, fetch_all=None) -> None: ...
    def get_calendar_by_uid(self, uid: str) -> Optional[CalendarData]: ...
    def get_events_by_calendar_uid(self, calendar_uid: str) -> list[EventData]: ...
    def get_event_by_global_uid(self, uid: str) -> Optional[EventData]: ...
    def get_occurrences_in_range(self, start, end, *, calendar_uid: str = "") -> list[Occurrence]: ...
    def add_event(self, event: EventData, calendar_uid: Optional[str] = None) -> EventData: ...
    def update_event(self, event: EventData) -> EventData: ...
    def delete_event_by_id(self, uid: str, calendar_uid: Optional[str] = None) -> bool: ...
```

Naming maps 1:1 onto the sibling (`task_list`->`calendar`, `task`->`event`), including `delete_event_by_id`'s `(uid, container_uid)` argument order and the env-var fallback for the container. Behaviors carried over from the sibling: `_adjust_url()` (scheme + Nextcloud `remote.php/dav/`), eager `_connect()` in `__init__` raising `ConnectionError`, `_fetch_raw_calendars()` filtering on `target_calendars` and `"VEVENT" in cal.get_supported_components()`, the `self.calendars` cache wiped and rebuilt by `load_remote_data()`, password never stored on `self`, `read_only` -> `PermissionError`, `debug` -> `pdb.post_mortem()`, per-event parse failures counted but not fatal, and the raw `cal.data` + `icalendar` `walk("VEVENT")` fallback when the primary fetch throws.

Event-specific behavior:

- Fetch via `calendar.search(start=..., end=..., event=True, expand=False)`. **`expand=False` is load-bearing**: server-side expansion returns RECURRENCE-ID instances, exactly the model we rejected, and it destroys the RRULE we want raw.
- Window resolution order: method arg -> ctor arg -> env var -> default. Ints resolve against `datetime.now(timezone.utc)` at each `load_remote_data()` call, so a long-lived instance re-slides.
- `update_event` bumps `sequence`, refreshes `changed_at`, PUTs `to_vcalendar()` to `event._href`, sets `synced = True`.
- `get_occurrences_in_range` flattens across the cache, sorted by `dtstart`.
- **The cache is a window, not the calendar**: `get_event_by_global_uid` can return `None` for an event that exists. `fetch_all=True` is the escape hatch. A recurring master whose DTSTART predates the window relies on the server honoring RFC 4791 time-range recurrence matching (Sabre and Nextcloud do).

Env vars, namespace `CALDAV_CAL_API_*`: `URL`, `USERNAME`, `PASSWORD`, `DEFAULT_CALENDAR_UID`, `WINDOW_START_DAYS`, `WINDOW_END_DAYS`, `FETCH_ALL`, `LOG_LEVEL`, plus `CALDAV_CAL_API_TEST_{URL,USERNAME,PASSWORD,CALENDAR_NAME}` for the test suite. Every var must appear in the code, in `.env.example`, and in both README and `docs/source/the_usage.md`.

## CLI (`caldav_cal_api/__main__.py`)

click group, `-h/--help`, entry point `caldav-cal-api = caldav_cal_api.__main__:cli`. Four commands, all read-only (writes stay library-only for v0.1):

| Command | Purpose |
| --- | --- |
| `list-calendars` | JSON `[{"name":..., "uid":...}]`, forced `read_only=True`. |
| `list-upcoming` | Next N events from now: `--calendar-uid`, `--limit`, `--days`, `--json`. Expands recurrences via `get_occurrences_in_range`. |
| `search TEXT` | `--start`/`--end` override the load window server-side, then case-insensitive substring match on summary/description/location client-side. `--json`. |
| `dump` | Raw `to_ical()` blocks with `#` comment headers, `--calendar-uid`. |

Shared `get_api(...)` helper, `--debug` calling `enable_debug_logging()` then `code.interact()` after the command, and the same `click.UsageError` / `ConnectionError` / generic exception ladder as the sibling. Unlike the sibling, the connection options come from **one shared decorator** rather than being retyped per command, and their help text names the real `CALDAV_CAL_API_*` vars.

## Packaging

`setup.py` mirroring the sibling's, `name="caldav-cal-api"`, url `https://github.com/thiswillbeyourgithub/Caldav-Cal-API/`. `install_requires`: `caldav`, `icalendar>=6.0` (needed for `Timezone.from_tzinfo`), `python-dateutil`, `click`, `urllib3`, `loguru`, `platformdirs`. Same `dev` extra. `python_requires=">=3.10"`. `bumpver.toml` with file_patterns pointing at `bumpver.toml`, `setup.py`, `caldav_cal_api/caldav_cal_api.py`, `docs/source/conf.py`.

## Warts in the sibling to NOT reproduce

- The Nextcloud summary-preservation retry dance in `add_task`/`update_task` (three fallback write strategies plus a fresh-fetch summary comparison). Nextcloud-Tasks-specific noise.
- `icalendar` imported but undeclared in `install_requires`.
- `python_requires=">=3.8"` while the code uses `X | None` and `list[...]` at runtime.
- `import random` in `utils/data.py`, unused.
- `html_theme = "pydata_sphinx_theme"` commented out in `docs/source/conf.py` (docs silently render with alabaster while `html_theme_options` sits inert); also a commented-out `autodoc-skip-member` hook.
- Stale CLI help text referring to `CALDAV_URL`.
- Docs referring to `delete_task(...)` when the method is `delete_task_by_id(...)`.
- README claiming a `WARNING` default log level when the code defaults to `INFO`.
- Missing `License ::` classifier.
- Connection options retyped in all five CLI commands instead of a shared decorator.
- Fields declared but never populated (`TaskListData.color`).
- `from_ical` setting `synced = True` merely because a UID was present: `synced` is a statement about the server, so only the API layer sets it.

Dropped as meaningless for events: `completed`, `percent_complete`, `due_date`, priority clamping, `parent`/`parent_task`/`child_tasks` (RELATED-TO hierarchy), `attachments`, `notified`, `trash`, `deleted`, and the `.complete()`/`.uncomplete()` calls. Note `STATUS:CANCELLED` is a published state other attendees must see, **not** a deletion: do not wire it to any delete flag. And do not copy the `"T" not in value` date-vs-datetime heuristic; `icalendar` reports `VALUE=DATE` structurally.

## Verification

**Offline (no server needed):**

- `to_ical -> from_ical -> to_ical` is byte-identical. Two explicit cases: an object built by hand with no `_raw_component` (first serialization must equal the second), and one where `_source_duration` round-trips as DURATION (the only value-dependent branch in the writer).
- Unknown-property preservation: parse a VEVENT carrying ATTENDEE, ORGANIZER and a VALARM, change only `summary`, re-serialize, assert all three survive verbatim.
- `all_day` invariant: each coercion branch in `__post_init__`; `dtend == dtstart` all-day raises `ValueError`; `last_day` returns the inclusive last day.
- Timezone: a `DTSTART;TZID=Europe/Paris` event lands as UTC-aware with `dtstart_tzid == "Europe/Paris"`, and `dtstart_local` renders back to the original wall-clock time.
- `get_occurrences`: weekly Europe/Paris rule spanning the March DST transition keeps 09:00 local at every instance; EXDATE removes an instance; RDATE adds one; all-day weekly expansion yields `date` objects; `limit` caps and warns; `end < start` raises.

**Against a real server** (real integration tests, no mocks, `python-dotenv` loading `.env`, session-scoped fixtures, `pytest.skip` when `CALDAV_CAL_API_TEST_*` are unset):

1. Fetch calendars, create an event, reload, assert count+1, update summary and location, reload and verify, delete it, assert count restored.
2. A recurring event created with an RRULE survives create -> reload -> `get_occurrences` -> update -> reload with the RRULE intact (the regression that matters most given `expand=False`).
3. An all-day event round-trips as `date` objects with the exclusive DTEND preserved.
4. Read-only mode: `add_event`/`update_event`/`delete_event_by_id` each raise `PermissionError` containing "read-only mode".
5. CLI smoke tests via `subprocess.run([sys.executable, "-m", "caldav_cal_api", ...], check=True)` for `list-calendars --json`, `list-upcoming`, `search`, and `dump` (asserting `BEGIN:VEVENT` in stdout).
6. Window behavior: load with a narrow window, assert a known out-of-window event is absent; reload with `fetch_all=True`, assert it appears.

**Manual:** `caldav-cal-api list-upcoming --days 7` against the real Nextcloud, eyeballing that the agenda matches the web UI including a recurring event and an all-day event.

## Open TODOs

None. As of 2026-09-10 the repo (`https://github.com/thiswillbeyourgithub/Caldav-Cal-API`), the PyPI package (`caldav-cal-api`) and the docs (`https://caldav-cal-api.readthedocs.io/en/latest/`) all exist, and every placeholder that was waiting on them has been filled in.
