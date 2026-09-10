# Usage

## Configuration

Every setting can come from an environment variable. Copy `.env.example` to `.env` as a starting point.

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

An explicit argument always beats the environment. For the load window the order is: the argument passed to `load_remote_data()`, then the one passed to the constructor, then the environment variable, then the built-in default.

## Command line

Two equivalent entry points:

```bash
caldav-cal-api <command>
python -m caldav_cal_api <command>
```

All four commands are read-only. Writing is available through the Python API only.

```bash
# List the calendars on the server (JSON)
caldav-cal-api list-calendars

# The next week's agenda, with recurring events expanded into occurrences
caldav-cal-api list-upcoming --days 7

# One calendar, as JSON
caldav-cal-api list-upcoming --calendar Personal --json

# Text search over a date range
caldav-cal-api search dentist --start 2026-01-01 --end 2026-12-31

# Raw VEVENTs, for diffing a calendar into version control or filing a bug report
caldav-cal-api dump --calendar Personal
```

### Shared options

Every command accepts:

- `--url`, `--username`, `--password`: override the corresponding environment variables.
- `--nextcloud-mode` / `--no-nextcloud-mode`: append Nextcloud's `remote.php/dav/` path when missing.
- `--calendar NAME_OR_ID`: repeatable. Restricting the calendars loaded is the single most effective way to speed up a command.
- `--debug` / `--no-debug`: verbose logging, then an interactive Python console with `api` in scope.
- `-h` / `--help`.

### Per-command options

- `list-calendars`: none. It deliberately does not load events, so it stays fast on large accounts.
- `list-upcoming`: `--calendar-uid`, `--days` (default 7), `--limit` (default 25), `--json`.
- `search TEXT`: `--calendar-uid`, `--start`, `--end` (both `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`), `--json`. The range is applied server-side as the load window; the text match then runs locally over summary, description and location. Narrowing the range is what makes a search over a large account fast.
- `dump`: `--calendar-uid`.

> **Note on read/write.** The CLI can never write. `CalendarAPI` itself accepts `read_only=True`, which makes `add_event`, `update_event` and `delete_event_by_id` raise `PermissionError`; that is the way to dry-run a script that would otherwise modify a calendar.
