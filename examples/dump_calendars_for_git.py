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
Dump every calendar to a directory of text files, one per event, for version control.

Committing the output on a schedule gives a readable history of a calendar: what was
added, moved or deleted, and when. One file per event keeps diffs small and meaningful,
which a single large .ics file does not.

Usage
-----
    python examples/dump_calendars_for_git.py --output ./calendar_dump
    python examples/dump_calendars_for_git.py --output ./calendar_dump --fetch-all
"""

import re
from pathlib import Path

import click
from dotenv import load_dotenv

from caldav_cal_api import CalendarAPI

# Credentials come from the environment. Reading .env here is what lets `uv run` work
# straight out of a checkout without exporting anything by hand.
load_dotenv()


def _safe_name(value: str) -> str:
    """Turn an arbitrary name into something usable as a filename."""
    cleaned = re.sub(r"[^\w.-]+", "_", value).strip("_")
    return cleaned or "unnamed"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--output",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory to write into. Created if missing.",
)
@click.option(
    "--fetch-all",
    is_flag=True,
    help="Dump every event rather than the default date window. Can be slow.",
)
@click.option(
    "--calendar",
    "target_calendars",
    multiple=True,
    help="Restrict to these calendar names or ids. Repeatable.",
)
def main(output: Path, fetch_all: bool, target_calendars: tuple) -> None:
    """Write one .ics file per event, under one directory per calendar."""
    api = CalendarAPI(
        target_calendars=list(target_calendars) or None,
        fetch_all=fetch_all,
        read_only=True,
    )
    api.load_remote_data()

    written = 0
    for calendar in api.calendars:
        calendar_dir = output / _safe_name(calendar.name)
        calendar_dir.mkdir(parents=True, exist_ok=True)

        for event in calendar.events:
            # Named by UID rather than summary: the UID is stable across renames, so a
            # renamed event shows up as a diff instead of a delete plus an add.
            path = calendar_dir / f"{_safe_name(event.uid)}.ics"
            path.write_text(event.to_ical(), encoding="utf-8")
            written += 1

        click.echo(f"{calendar.name}: {len(calendar.events)} event(s)")

    click.echo(f"\nWrote {written} file(s) to {output}.")
    click.echo(
        "Note: events removed on the server are not deleted here; commit after a"
    )
    click.echo("`git rm -r` of the output directory if you want deletions to show up.")


if __name__ == "__main__":
    main()
