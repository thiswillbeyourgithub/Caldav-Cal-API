#!/usr/bin/env python3
"""
Dump every calendar to a single text file per calendar, for git versioning.

The calendar counterpart of the sibling caldav_tasks_api project's
``dump_all_lists_for_git.py``: one ``{calendar_name}.dump`` file per calendar, holding
every event in that calendar as a VEVENT block under a short ``#`` header. Commit the
output directory on a schedule and ``git log -p`` becomes a readable history of your
calendars: what was added, moved, renamed or deleted, and when.

Two things differ from the tasks version, both because events are not tasks.

**Events are sorted by start time, not by modification date.** The tasks script appends
recently touched items at the bottom, which works when a list is a flat backlog. A
calendar reads as a timeline, so chronological order keeps a re-dump stable: editing one
event changes one hunk instead of moving a block to the end of the file.

**Only a date window is dumped.** A calendar can hold decades of events, so the library
loads a bounded range (by default the last 30 days and the next year). Widen it with
``--days-back`` and ``--days-forward``, or pass ``--fetch-all`` to dump everything.

See also ``dump_calendars_for_git.py``, which writes one ``.ics`` file per event instead.
Per-event files give the smallest possible diffs and survive reordering; the single file
per calendar produced here is easier to read and to grep.

Usage
-----
python examples/dump_all_calendars_for_git.py --output-dir ./calendar_dump
python examples/dump_all_calendars_for_git.py --output-dir ./calendar_dump --fetch-all
python examples/dump_all_calendars_for_git.py --output-dir ./calendar_dump --calendar Perso
"""

import datetime
from pathlib import Path
from typing import Optional

import click

from caldav_cal_api import CalendarAPI, CalendarData, EventData

UTC = datetime.timezone.utc


def sanitize_filename(name: str) -> str:
    """Turn a calendar name into something safe to use as a filename.

    Parameters
    ----------
    name : str
        The original calendar name.

    Returns
    -------
    str
        A sanitized name, safe on most filesystems.
    """
    invalid_chars = ["<", ">", ":", '"', "/", "\\", "|", "?", "*"]
    sanitized = name
    for char in invalid_chars:
        sanitized = sanitized.replace(char, "_")

    sanitized = sanitized.strip(" .")

    if not sanitized:
        sanitized = "unnamed_calendar"

    return sanitized


def sort_events_chronologically(events: list[EventData]) -> list[EventData]:
    """Sort events by start time, earliest first, breaking ties on UID.

    Parameters
    ----------
    events : list of EventData
        The events to sort.

    Returns
    -------
    list of EventData
        The same events, in a stable chronological order.

    Notes
    -----
    An all-day event carries a ``date`` and a timed one a ``datetime``, which cannot be
    compared to each other, so both are projected onto a UTC datetime for the sort. The
    UID tiebreak is what makes the order total: without it, two events starting at the
    same instant could swap places between runs and produce a spurious diff.
    """

    def sort_key(event: EventData) -> tuple:
        start = event.dtstart
        if start is None:
            # An event with no DTSTART is malformed; keep it first rather than crashing.
            return (datetime.datetime.min.replace(tzinfo=UTC), event.uid)
        if isinstance(start, datetime.datetime):
            return (start.astimezone(UTC), event.uid)
        return (
            datetime.datetime.combine(start, datetime.time.min, tzinfo=UTC),
            event.uid,
        )

    return sorted(events, key=sort_key)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--url",
    envvar="CALDAV_CAL_API_URL",
    help="CalDAV server URL. Can be set via CALDAV_CAL_API_URL.",
)
@click.option(
    "--username",
    envvar="CALDAV_CAL_API_USERNAME",
    help="Username for CalDAV authentication. Can be set via CALDAV_CAL_API_USERNAME.",
)
@click.option(
    "--password",
    envvar="CALDAV_CAL_API_PASSWORD",
    help="Password for CalDAV authentication. Can be set via CALDAV_CAL_API_PASSWORD.",
)
@click.option(
    "--nextcloud-mode/--no-nextcloud-mode",
    default=True,
    help="Enable Nextcloud-specific URL handling (default: enabled).",
)
@click.option(
    "--debug/--no-debug", default=False, help="Enable debug mode (default: disabled)."
)
@click.option(
    "--ssl-verify/--no-ssl-verify",
    default=True,
    help="Verify SSL certificates (default: enabled).",
)
@click.option(
    "--calendar",
    "target_calendars",
    multiple=True,
    help="Restrict to these calendar names or ids. Repeatable.",
)
@click.option(
    "--days-back",
    type=int,
    default=None,
    help="How far back to dump, in days (default: 30).",
)
@click.option(
    "--days-forward",
    type=int,
    default=None,
    help="How far forward to dump, in days (default: 365).",
)
@click.option(
    "--fetch-all",
    is_flag=True,
    help="Dump every event, ignoring the date window. Can be slow on a large account.",
)
@click.option(
    "--output-dir",
    type=click.Path(exists=False, file_okay=False, dir_okay=True, path_type=Path),
    default=Path("."),
    help="Directory to save dump files (default: current directory).",
)
def main(
    url: Optional[str],
    username: Optional[str],
    password: Optional[str],
    nextcloud_mode: bool,
    debug: bool,
    ssl_verify: bool,
    target_calendars: tuple,
    days_back: Optional[int],
    days_forward: Optional[int],
    fetch_all: bool,
    output_dir: Path,
) -> None:
    """Dump all CalDAV calendars to individual files for git versioning.

    For each calendar found on the server, this script writes a file named
    "{calendar_name}.dump" containing every event in that calendar in VEVENT format,
    sorted by start time.
    """
    try:
        api = CalendarAPI(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            debug=debug,
            ssl_verify_cert=ssl_verify,
            target_calendars=list(target_calendars) or None,
            # Negative because the window start is an offset from now, not a duration.
            window_start=-abs(days_back) if days_back is not None else None,
            window_end=days_forward,
            fetch_all=fetch_all,
            read_only=True,  # We are only reading data.
        )

        click.echo("Loading data from the CalDAV server...")
        api.load_remote_data()

        if not api.calendars:
            click.echo("No calendars found on the server.")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Found {len(api.calendars)} calendar(s). Processing...")

        for calendar in api.calendars:
            process_calendar(calendar, output_dir)

        click.echo(f"Successfully dumped all calendars to {output_dir}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def process_calendar(calendar: CalendarData, output_dir: Path) -> None:
    """Dump a single calendar's events to a file.

    Parameters
    ----------
    calendar : CalendarData
        The calendar to dump.
    output_dir : Path
        Directory in which to write the dump file.
    """
    calendar_name = calendar.name or f"calendar_{calendar.uid}"
    click.echo(f"Processing calendar: {calendar_name} ({len(calendar.events)} events)")

    if not calendar.events:
        click.echo(f"  No events found in calendar '{calendar_name}'")
        # Still write the file, so that an emptied calendar shows up as a diff rather
        # than as an unchanged stale dump.
        output_content = (
            f"# Calendar: {calendar_name}\n"
            f"# UID: {calendar.uid}\n"
            f"# No events found\n"
        )
    else:
        vevent_blocks = []
        for event in sort_events_chronologically(calendar.events):
            try:
                vevent_blocks.append(event.to_ical())
            except Exception as e:
                click.echo(
                    f"  Warning: Failed to convert event '{event.summary}' to iCal: {e}"
                )
                continue

        output_content = (
            f"# Calendar: {calendar_name}\n"
            f"# UID: {calendar.uid}\n"
            f"# Events: {len(vevent_blocks)}\n\n"
        )
        output_content += "\n\n".join(vevent_blocks)
        output_content += "\n"

    filename = f"{sanitize_filename(calendar_name)}.dump"
    output_path = output_dir / filename

    try:
        # newline="" keeps the CRLF line endings iCalendar mandates, instead of letting
        # the text layer rewrite them and produce a whole-file diff on another platform.
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(output_content)
        click.echo(f"  Saved to: {output_path}")
    except Exception as e:
        click.echo(f"  Error saving to {output_path}: {e}", err=True)


if __name__ == "__main__":
    main()
