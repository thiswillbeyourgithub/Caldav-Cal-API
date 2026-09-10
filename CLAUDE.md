# CalDAV-Cal-API - notes for agents

Written with Claude Code, which also wrote the whole library. This file was originally the implementation plan; the plan is done, so what remains here is the reasoning behind the design and the traps that cost real debugging time. The code is authoritative for signatures and field lists, so none are duplicated here.

## What this is

A CalDAV client for **VEVENT** calendars: read, create and modify calendars through plain Python objects, plus a small read-only CLI.

It deliberately mirrors the sibling project `caldav_tasks_api` (at `../caldav_tasks_api_repo`, AGPLv3, same author), which does the same for **VTODO** tasks. Naming maps 1:1 (`task_list` -> `calendar`, `task` -> `event`), including argument order, so the two libraries feel like one from the outside. When adding something, check how the sibling spells it first.

## Locked decisions

| Topic | Decision |
| --- | --- |
| Component scope | VEVENT only. No VJOURNAL, no free/busy. |
| Field scope | Core fields only. No attendees/organizer/alarms as first-class fields. |
| Datetimes | Timezone-aware, normalized to UTC internally, original TZID preserved in a companion field so writes round-trip. All-day stays `date` + `all_day` flag. |
| Recurrence | `RRULE`/`RDATE`/`EXDATE` stored raw, plus one opt-in `get_occurrences(start, end)` helper using `dateutil`. No `RECURRENCE-ID` override objects, no "this and future" edit semantics. |
| Serialization | The `icalendar` library, **not** the sibling's hand-rolled string concatenation. |
| Raw retention | Keep a private `_raw_component`; `to_ical()` rewrites only a managed property set. Diverges from the sibling, see below. |
| Load scope | Date window via caldav search, default -30d/+365d, configurable by ctor args and env vars, with a `fetch_all` escape hatch. |
| CLI | Four read-only commands: `list-calendars`, `list-upcoming`, `search`, `dump`. Writes stay library-only. |
| Docstrings | NumPy style (per the user's dev-pref), unlike the sibling's Google style. |
| Not doing | No `create_calendar`/`delete_calendar`. |

### Why raw retention diverges from the sibling

The sibling can afford to regenerate VTODO from scratch because VTODO carries little beyond what `TaskData` models. VEVENT does not. Having declined to model attendees, organizer and alarms, a non-retaining `to_ical()` would make every `update_event()` a silent data loss: change a summary, strip the meeting's participants and everyone's reminders. `_raw_component` is `repr=False, compare=False`, so `__eq__` still compares modeled fields only.

## Orientation

- `caldav_cal_api/caldav_cal_api.py` - `CalendarAPI`: connection, the windowed cache, the write methods. `VERSION` lives here and is bumpver-tracked.
- `caldav_cal_api/utils/data.py` - `CalendarData`, `EventData`, `Occurrence`, `XProperties`. All the iCalendar and timezone logic.
- `caldav_cal_api/__main__.py` - the click CLI, entry point `caldav-cal-api`.
- `caldav_cal_api/utils/logging_config.py` - loguru setup, run at import. Ported near-verbatim from the sibling.
- `tests/test_event_data.py`, `tests/test_occurrences.py`, `tests/test_etag_normalization.py` - offline, run in a bare checkout.
- `tests/test_cal_api.py` - server-backed, skipped without credentials.

`XProperties` and `logging_config.py` are ported from the sibling with the app name changed to `caldav-cal-api` and the env var to `CALDAV_CAL_API_LOG_LEVEL`.

## Invariants worth not breaking

**`all_day` is derived from the value type, coercively.** `all_day is True` iff `dtstart` is a `date` that is not a `datetime`. Every check must use `isinstance(x, datetime.datetime)` and never `isinstance(x, datetime.date)`, because a datetime *is* a date. `__post_init__` coerces rather than raises, since a dataclass cannot tell "the caller passed False" from "the default applied". All-day implies both TZIDs forced to `""`.

**DTEND is exclusive.** A one-day all-day event on 2026-03-04 has `dtend=date(2026, 3, 5)`. `last_day` exists so callers are not tempted to "fix" this. An all-day event whose `dtend == dtstart` describes zero days and is refused; a *timed* event with equal ends is an instant and is accepted (see gotchas).

**`expand=False` on the server search is load-bearing.** Server-side expansion returns RECURRENCE-ID instances, exactly the model that was rejected, and it destroys the RRULE we want to keep raw.

**Expansion runs in the original TZID zone, then converts back to UTC.** "Every Monday 09:00 Europe/Paris" expanded over UTC instants drifts an hour at each DST transition. This is the entire reason `dtstart_tzid` exists.

**`to_ical()` rewrites only the managed property set** (UID, SUMMARY, DESCRIPTION, LOCATION, STATUS, CATEGORIES, DTSTART, DTEND, DURATION, RRULE, RDATE, EXDATE, DTSTAMP, LAST-MODIFIED, SEQUENCE, plus every `X-`). Everything else is re-emitted verbatim from `_raw_component`. Both construction paths funnel through one `_build_component()` that writes the managed set in a fixed order, which is what makes `to_ical -> from_ical -> to_ical` byte-identical. Output is CRLF; tests assert that.

**Never mint data for a component that came from a server.** `uid` gets a uuid4 only when `_raw_component is None`; `changed_at` (LAST-MODIFIED, optional in the spec) likewise. Minting either forks a server object or makes parsing non-deterministic. `synced` is a statement about the server, so only the API layer sets it, never `from_ical`.

**`get_occurrences` returns `Occurrence`, not `EventData` clones.** Clones would each carry the master's UID, corrupting any uid-keyed cache and inviting `update_event()` on a phantom with no server representation.

**The cache is a window, not the calendar.** `get_event_by_global_uid()` can return `None` for an event that really exists. `fetch_all=True` is the escape hatch. Say so in any docstring that could mislead.

## Gotchas that cost real debugging time

**ETags get mangled by HTTP compression.** `caldav` sends the ETag it holds as `If-Match` on every write. Apache appends the content coding it applied to the ETag of any response it compresses, so a large enough GET hands back `"abc-gzip"` while the server stores `"abc"`, and the write fails with 412. Only the *header* is rewritten: the WebDAV `getetag` property travels inside the XML body of a PROPFIND and arrives intact, which is what `_normalize_etag` uses. Do not go back to trimming a list of suffixes: which coding appears depends on what the client negotiated, so the same server returned `-gzip` to a plain urllib3 and `-zstd` to an environment with urllib3-future and brotli.

**Size is what triggered it**, which is why it looked recurrence-specific: a `DTSTART;TZID=` forces a VTIMEZONE into the payload and pushes it past the compression threshold.

**Real calendars violate RFC 5545 constantly, and the library reads other people's calendars.** Two cases found in one live Nextcloud account, both of which silently lost data before being fixed: a timed event with `DTEND == DTSTART` (rejected, so the event vanished from the view), and `UNTIL` as a bare DATE beside a timezone-aware DTSTART (dateutil raises, so expansion fell back to the master alone and the event read as though it had stopped repeating). Prefer accommodating the input over being right about the spec, and never rewrite the stored value: accommodation happens at read time so what goes back to the server stays byte-for-byte what came from it.

**An RRULE with no COUNT or UNTIL is infinite.** `list(rule_set)` never returns. Expansion must go through `rruleset.between(after=..., before=...)`, with the lower bound pulled back by the event's duration so an instance that started before the window but is still running inside it is not missed.

**`DTSTART;TZID=` without a matching VTIMEZONE is invalid** and some servers reject it, so `to_vcalendar()` (not `to_ical()`) is what gets PUT. `icalendar >= 6.0` synthesizes the VTIMEZONE via `Timezone.from_tzinfo`.

**DTSTAMP must be settled at construction, not at write time**, or `to_ical()` is not a pure function of the object and the round-trip guarantee fails.

## Conventions

- Black, via pre-commit. NumPy-style docstrings with napoleon.
- `bumpver` updates the version in four files: `bumpver.toml`, `setup.py`, `caldav_cal_api/caldav_cal_api.py`, `docs/source/conf.py`.
- Env vars live in the `CALDAV_CAL_API_*` namespace. Any new one must be wired end to end: read in the code, listed in `.env.example`, and documented in both `README.md` and `docs/source/the_usage.md`.
- Markdown: one line per paragraph, no hard wrapping inside a paragraph.
- `LICENSE` is the sibling's AGPLv3 file, copied verbatim at the user's request. Do not generate a license file.

## Running the tests

```bash
uv pip install -e ".[dev]"
pytest                        # offline tests only, without credentials
```

The server-backed tests need `CALDAV_CAL_API_TEST_URL`, `_USERNAME`, `_PASSWORD` and `_CALENDAR_NAME` (read from `.env`). **`_CALENDAR_NAME` must point at a scratch calendar**: those tests create and delete events in it. The user's account also holds real calendars that must never be written to.

Worth knowing: the user runs the suite in a second, system-wide virtualenv (python 3.13, caldav 3.2.0, urllib3-future plus brotli) as well as the repo's own (python 3.11, caldav 3.2.1). Those two negotiate different HTTP content codings, which is exactly how the ETag bug above escaped the first fix. Check both before calling a server-facing fix done.

Docs build with warnings as errors:

```bash
cd docs && python -m sphinx -b html -W source "$TMPDIR/docbuild"
```

Examples are `uv run`-nable through PEP 723 inline metadata, so `uv run examples/weekly_agenda.py --days 7` works in a bare checkout. The path source in each script's metadata points at the checkout and should be dropped once that is no longer wanted.

## Warts in the sibling not to reproduce

- The Nextcloud summary-preservation retry dance in `add_task`/`update_task` (three fallback write strategies plus a fresh-fetch summary comparison). Nextcloud-Tasks-specific noise.
- `icalendar` imported but undeclared in `install_requires`.
- `python_requires=">=3.8"` while the code uses `X | None` and `list[...]` at runtime.
- `html_theme = "pydata_sphinx_theme"` commented out in `docs/source/conf.py`, so the docs silently render with alabaster while `html_theme_options` sits inert.
- Stale CLI help text naming env vars that do not exist.
- Docs referring to `delete_task(...)` when the method is `delete_task_by_id(...)`.
- README claiming a default log level the code does not use.
- Missing `License ::` classifier.
- Connection options retyped in every CLI command instead of one shared decorator.
- Fields declared but never populated (`TaskListData.color`).

Dropped as meaningless for events: `completed`, `percent_complete`, `due_date`, priority clamping, `parent`/`parent_task`/`child_tasks` (RELATED-TO hierarchy), `attachments`, `notified`, `trash`, `deleted`, and `.complete()`/`.uncomplete()`. Note that `STATUS:CANCELLED` is a published state other attendees must see, **not** a deletion: do not wire it to any delete flag. And do not copy the `"T" not in value` date-vs-datetime heuristic; `icalendar` reports `VALUE=DATE` structurally.
