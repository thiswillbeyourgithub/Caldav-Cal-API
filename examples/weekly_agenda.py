"""
Print the coming week's agenda, grouped by day.

Recurring events are expanded, so a weekly standup appears on every day it actually
occurs rather than once at its original start.

Usage
-----
    python examples/weekly_agenda.py
    python examples/weekly_agenda.py --days 14 --calendar Personal

Credentials come from CALDAV_CAL_API_URL / _USERNAME / _PASSWORD.
"""

import datetime
from collections import defaultdict

import click

from caldav_cal_api import CalendarAPI

UTC = datetime.timezone.utc


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--days", default=7, show_default=True, help="How far ahead to look.")
@click.option(
    "--calendar",
    "target_calendars",
    multiple=True,
    help="Restrict to these calendar names or ids. Repeatable.",
)
def main(days: int, target_calendars: tuple) -> None:
    """Print the coming week's agenda."""
    now = datetime.datetime.now(UTC)
    end = now + datetime.timedelta(days=days)

    api = CalendarAPI(
        target_calendars=list(target_calendars) or None,
        # Reach well into the past so a recurring event whose master started long ago
        # is still loaded, and only slightly into the future beyond the window shown.
        window_start=-365,
        window_end=days + 1,
        read_only=True,
    )
    api.load_remote_data()

    by_day = defaultdict(list)
    for occurrence in api.get_occurrences_in_range(now, end):
        # Group in local time: an event at 00:30 UTC may well belong to the previous day
        # for the person reading the agenda.
        if occurrence.all_day:
            day = occurrence.dtstart
        else:
            day = occurrence.dtstart.astimezone().date()
        by_day[day].append(occurrence)

    if not by_day:
        click.echo(f"Nothing scheduled in the next {days} day(s).")
        return

    for day in sorted(by_day):
        click.echo(click.style(day.strftime("%A %d %B %Y"), bold=True))
        for occurrence in by_day[day]:
            if occurrence.all_day:
                when = "all day"
            else:
                when = occurrence.dtstart.astimezone().strftime("%H:%M")
            location = (
                f"  ({occurrence.event.location})" if occurrence.event.location else ""
            )
            click.echo(f"  {when:>7}  {occurrence.event.summary}{location}")
        click.echo()


if __name__ == "__main__":
    main()
