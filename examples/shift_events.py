#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "caldav-cal-api",
#     "python-dotenv>=1.0",
# ]
#
# # The path source makes the script run against this checkout, so `uv run` needs no
# # install step. Drop this block once caldav-cal-api is published to PyPI; the
# # dependency above is then enough, and the script becomes copy-pasteable anywhere.
# [tool.uv.sources]
# caldav-cal-api = { path = "../", editable = true }
# ///
"""
Shift every event matching a text search by a fixed amount of time.

The use case this exists for: a recurring meeting moves by half an hour, or a trip is
postponed by a day and every event booked around it has to follow.

Defaults to a dry run. Nothing is written until --apply is passed, and the dry run works
by opening the API read-only, so a mistake in this script cannot touch the server.

Usage
-----
    python examples/shift_events.py "Standup" --minutes 30
    python examples/shift_events.py "Standup" --minutes 30 --apply
    python examples/shift_events.py "Trip" --days 1 --apply
"""

import datetime

import click
from dotenv import load_dotenv

from caldav_cal_api import CalendarAPI

# Credentials come from the environment. Reading .env here is what lets `uv run` work
# straight out of a checkout without exporting anything by hand.
load_dotenv()

UTC = datetime.timezone.utc


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("text")
@click.option("--days", default=0, help="Days to add (may be negative).")
@click.option("--minutes", default=0, help="Minutes to add (may be negative).")
@click.option(
    "--calendar",
    "target_calendars",
    multiple=True,
    help="Restrict to these calendar names or ids. Repeatable.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Actually write the changes. Without this, nothing is modified.",
)
def main(
    text: str, days: int, minutes: int, target_calendars: tuple, apply_changes: bool
) -> None:
    """Shift events whose summary contains TEXT."""
    delta = datetime.timedelta(days=days, minutes=minutes)
    if not delta:
        raise click.UsageError("Pass a non-zero --days and/or --minutes.")

    api = CalendarAPI(
        target_calendars=list(target_calendars) or None,
        # read_only is the dry run: every write raises PermissionError, so a bug here
        # cannot modify the calendar.
        read_only=not apply_changes,
    )
    api.load_remote_data()

    needle = text.lower()
    matches = [
        event
        for calendar in api.calendars
        for event in calendar.events
        if needle in event.summary.lower() and event.dtstart is not None
    ]

    if not matches:
        click.echo(f"No event matching '{text}' in the loaded window.")
        return

    for event in matches:
        old_start = event.dtstart
        # dtend must move first: __post_init__ enforces dtend > dtstart, and assigning a
        # later start before a later end would momentarily violate that. Assigning the
        # fields directly bypasses __post_init__, but keeping the order right means the
        # object is never observed in an inconsistent state.
        if event.dtend is not None:
            event.dtend = event.dtend + delta
        event.dtstart = old_start + delta

        arrow = "->" if apply_changes else "would become"
        click.echo(f"{old_start}  {arrow}  {event.dtstart}   {event.summary}")

        if apply_changes:
            api.update_event(event)

    if not apply_changes:
        click.echo(
            f"\nDry run: {len(matches)} event(s) would move. Pass --apply to write."
        )


if __name__ == "__main__":
    main()
