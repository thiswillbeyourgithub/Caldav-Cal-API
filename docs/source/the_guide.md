# Guide

## Python API

### `CalendarAPI.__init__`

```python
CalendarAPI(
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    nextcloud_mode: bool = True,
    debug: bool = False,
    target_calendars: list[str] | None = None,
    read_only: bool = False,
    ssl_verify_cert: bool = True,
    window_start: int | datetime.datetime | None = None,
    window_end: int | datetime.datetime | None = None,
    fetch_all: bool = False,
)
```

Connects immediately and fetches the calendar list, so a bad URL or password fails here rather than later.

**Parameters**

- `url`, `username`, `password`: fall back to `CALDAV_CAL_API_URL`, `..._USERNAME`, `..._PASSWORD`. The password is used for the connection and never stored on the instance.
- `nextcloud_mode`: append `remote.php/dav/` to the URL when it is missing. A scheme is added too if you omit it.
- `debug`: drop into `pdb.post_mortem()` on unexpected failures.
- `target_calendars`: restrict to these calendar names or ids. The most effective single speedup.
- `read_only`: make every write raise `PermissionError`.
- `ssl_verify_cert`: verify the server certificate.
- `window_start`, `window_end`: the load window. An `int` is a day offset from now (negative for the past); a `datetime` is used as-is. Defaults are -30 and +365 days.
- `fetch_all`: ignore the window and load every event.

**Raises**: `ValueError` for missing credentials, `ConnectionError` if the server cannot be reached.

### `load_remote_data(*, window_start=None, window_end=None, fetch_all=None)`

Replaces the in-memory cache with a fresh read from the server. All three arguments override the constructor for this call only.

Day offsets are resolved against *now* at call time, so a long-lived instance re-slides its window on each reload rather than drifting into the past.

Events are fetched with server-side expansion off. Expansion would return one component per occurrence, each carrying a `RECURRENCE-ID`, and would destroy the `RRULE` that `get_occurrences()` needs.

### Lookups

- `get_calendar_by_uid(uid) -> CalendarData | None`
- `get_events_by_calendar_uid(calendar_uid) -> list[EventData]`
- `get_event_by_global_uid(uid) -> EventData | None`, searching every loaded calendar. Returns `None` for an event outside the loaded window even though it exists on the server.
- `get_occurrences_in_range(start, end, *, calendar_uid="") -> list[Occurrence]`, expanding every loaded event and returning the result sorted by start time. Purely local: it can only expand what `load_remote_data()` already fetched.

### Writes

- `add_event(event, calendar_uid=None) -> EventData`. The target calendar is taken from the argument, then the event's `calendar_uid`, then `CALDAV_CAL_API_DEFAULT_CALENDAR_UID`. The event is updated in place with the server's authoritative UID and marked `synced`.
- `update_event(event) -> EventData`. Requires both a UID and a calendar UID. `SEQUENCE` is incremented on every update, because other CalDAV clients are entitled to ignore a revision whose `SEQUENCE` did not advance.
- `delete_event_by_id(uid, calendar_uid=None) -> bool`. The event's content is logged before deletion, so a mistake stays recoverable from the log file.

All three raise `PermissionError` when the API is read-only.

## Data structures

### `CalendarData`

A calendar collection: `uid`, `name`, `color`, `deleted`, `synced`, and `events`. Iterating a `CalendarData` iterates its events, and `len()` counts them.

### `EventData`

The modeled properties:

| Field | iCal property | Notes |
| --- | --- | --- |
| `uid` | `UID` | Minted for new events, never for parsed ones |
| `summary` | `SUMMARY` | |
| `description` | `DESCRIPTION` | |
| `location` | `LOCATION` | |
| `status` | `STATUS` | `TENTATIVE`, `CONFIRMED` or `CANCELLED` |
| `categories` | `CATEGORIES` | |
| `dtstart`, `dtstart_tzid` | `DTSTART` | UTC-normalized value plus its original TZID |
| `dtend`, `dtend_tzid` | `DTEND` | Exclusive |
| `all_day` | `DTSTART;VALUE=DATE` | Mirrors `dtstart`'s type |
| `rrule` | `RRULE` | Raw value string |
| `rdate`, `exdate` | `RDATE`, `EXDATE` | Raw content lines |
| `sequence` | `SEQUENCE` | |
| `created_at` | `DTSTAMP` | |
| `changed_at` | `LAST-MODIFIED` | |
| `x_properties` | `X-*` | See the FAQ |
| `calendar_uid` | (none) | Owning calendar |
| `synced` | (none) | Set by `CalendarAPI` only |

Convenience properties: `dtstart_local` and `dtend_local` (rendered back into the original zone), `effective_dtend` (applying the RFC 5545 defaults when `DTEND` is absent), `duration`, `last_day` (the inclusive final day of an all-day event) and `is_recurring`.

Methods: `to_ical()`, `to_vcalendar()`, `to_dict()`, `get_occurrences(start, end, *, limit=1000)`, `delete()`, and the static `from_ical(ical, calendar_uid="")`.

Anything not in the table above is preserved verbatim through the retained source component. See the FAQ.

### `Occurrence`

A frozen dataclass with `dtstart`, `dtend`, `all_day` and `event` (the master it came from), plus a `summary` shortcut and `overlaps(window_start, window_end)`. It is a read-only view: it has no server representation and cannot be written back.

### `XProperties`

Attribute-style access to custom `X-` properties, preserving raw keys. See the FAQ.
