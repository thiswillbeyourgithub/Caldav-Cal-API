# FAQ

## When does anything actually reach the server?

Only in `add_event()`, `update_event()` and `delete_event_by_id()` (and `EventData.delete()`, which calls the last one). Mutating an `EventData` in memory changes nothing remotely; you must call `update_event()`. There is no autosave and no background sync.

The `synced` flag is a statement about the server, so only `CalendarAPI` ever sets it. Parsing iCalendar text never marks anything synced, and editing a field does not clear the flag.

## Why does `get_event_by_global_uid()` return `None` for an event I can see in my calendar app?

Because the in-memory cache holds a **time window, not the whole calendar**. `load_remote_data()` fetches the last 30 days and the next year by default. An event outside that range was never loaded.

Widen the window, or load everything:

```python
api.load_remote_data(window_start=-365, window_end=1095)
api.load_remote_data(fetch_all=True)  # Everything, can be slow
```

A recurring event whose `DTSTART` is older than the window still appears as long as one of its occurrences falls inside it: RFC 4791 requires servers to match time ranges against expanded occurrences, and Sabre-based servers (Nextcloud, ownCloud) do this correctly.

## How are recurring events handled?

The rule is stored raw. `event.rrule` is a string like `"FREQ=WEEKLY;BYDAY=WE"`, and `event.rdate` / `event.exdate` are lists of whole content lines. Nothing is expanded until you ask:

```python
for occurrence in event.get_occurrences(start, end):
    print(occurrence.dtstart, occurrence.dtend)
```

Expansion runs in the event's original timezone and only then converts back to UTC, so "every Monday at 09:00 Europe/Paris" stays at 09:00 local on both sides of a DST transition. Expanding over UTC instants, which is the obvious implementation, drifts by an hour at every switch.

An `Occurrence` is a read-only view, not an `EventData`. It cannot be updated or deleted; it would carry the master's UID and there is no server object behind it.

## Can I edit a single occurrence of a recurring event?

No. Per-occurrence modifications are `RECURRENCE-ID` overrides, and this library models the master event only. When a payload contains overrides they are dropped with a warning rather than silently, so you will see it in the log. There is no "this event / this and future / all events" distinction.

## Why is `dtend` the day *after* my all-day event ends?

Because `DTEND` is exclusive in iCalendar. A single all-day event on 4 March is `dtstart=date(2026, 3, 4)`, `dtend=date(2026, 3, 5)`. Setting `dtend == dtstart` raises `ValueError` rather than creating a zero-length day, because that is almost always a bug.

Use `event.last_day` when displaying a range:

```python
print(f"{event.dtstart} to {event.last_day}")  # 2026-03-04 to 2026-03-04
```

## Will updating an event destroy its attendees or reminders?

No. Only a core set of properties is modeled, but each event keeps its source component, and writing rewrites only the properties this library owns. `ATTENDEE`, `ORGANIZER`, `VALARM`, `TRANSP`, `CLASS` and anything else the server sent are re-emitted verbatim.

The consequence is that you cannot change those properties through this library either. If you need to, reach into `event._raw_component`, which is an `icalendar.Event`.

## What happens to a timezone my system does not know?

It is logged as a warning and the value is treated as UTC. One exotic `TZID` must not abort a whole calendar load.

## Why is `to_ical()` output using CRLF line endings?

Because RFC 5545 requires it and the `icalendar` library is correct about it. The sibling `caldav_tasks_api` hand-writes LF-separated lines; do not copy assertions between the two test suites without adjusting for this.

## How do X- properties work?

Through the `XProperties` wrapper, ported from the sibling project. Raw keys are preserved exactly as the server sent them, and lookups accept a normalized form:

```python
event.x_properties["X-APPLE-SORT-ORDER"]  # By raw key, case-insensitive
event.x_properties.apple_sort_order        # By normalized attribute
"X-APPLE-SORT-ORDER" in event.x_properties
```

Note it has no `.get()` method: use `in` then `[]`.
