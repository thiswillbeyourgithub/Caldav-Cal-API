"""
Command-line interface for caldav-cal-api.

Run as ``caldav-cal-api <command>`` after installation, or ``python -m caldav_cal_api
<command>`` from a checkout.

Every command is read-only: this release exposes reading and searching from the terminal,
while creating and modifying events stays in the Python API where the caller can see what
is about to change.

Unlike the sibling caldav_tasks_api, which retypes the connection options in each command,
they live in one decorator here (``connection_options``) so a new command cannot drift out
of sync with the others.
"""

import code
import datetime
import json
from typing import Optional

import click
from loguru import logger

from caldav_cal_api.caldav_cal_api import CalendarAPI
from caldav_cal_api.utils.data import Occurrence
from caldav_cal_api.utils.logging_config import enable_debug_logging

UTC = datetime.timezone.utc

# click parses these into datetimes; a bare date is the common case, the full form is
# there for when a window boundary matters to the hour.
_DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]


def connection_options(func):
    """Attach the shared connection options to a command.

    Notes
    -----
    Defined once rather than per command so that the help text, the env var names and the
    defaults cannot diverge between commands as the CLI grows.
    """
    options = [
        click.option(
            "--url",
            default=None,
            help="CalDAV server URL. Defaults to the CALDAV_CAL_API_URL env var.",
        ),
        click.option(
            "--username",
            default=None,
            help="CalDAV username. Defaults to the CALDAV_CAL_API_USERNAME env var.",
        ),
        click.option(
            "--password",
            default=None,
            help="CalDAV password. Defaults to the CALDAV_CAL_API_PASSWORD env var.",
        ),
        click.option(
            "--nextcloud-mode/--no-nextcloud-mode",
            default=True,
            help="Append Nextcloud's remote.php/dav/ path to the URL when missing.",
        ),
        click.option(
            "--calendar",
            "target_calendars",
            multiple=True,
            help="Restrict to these calendar names or ids. Repeatable, and the most "
            "effective way to speed up loading.",
        ),
        click.option(
            "--debug/--no-debug",
            default=False,
            help="Verbose logging, then an interactive console with 'api' in scope.",
        ),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def get_api(
    *,
    url: Optional[str],
    username: Optional[str],
    password: Optional[str],
    nextcloud_mode: bool,
    target_calendars: tuple,
    debug: bool,
    **window_kwargs,
) -> CalendarAPI:
    """Build a read-only :class:`CalendarAPI` from CLI options.

    Raises
    ------
    click.UsageError
        When the configuration is incomplete, so the user sees a usage error rather than
        a traceback.
    """
    try:
        return CalendarAPI(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            target_calendars=list(target_calendars) or None,
            debug=debug,
            # Every CLI command reads; nothing here should ever be able to write.
            read_only=True,
            **window_kwargs,
        )
    except ValueError as ve:
        logger.error(f"Configuration error: {ve}")
        raise click.UsageError(str(ve))


def _drop_into_console(local_scope: dict) -> None:
    """Open an interactive console with the command's locals available."""
    click.echo("Debug mode: starting interactive console. API available as 'api'.")
    scope = globals().copy()
    scope.update(local_scope)
    code.interact(local=scope)


def _format_occurrence(occurrence: Occurrence) -> str:
    """Render one occurrence as a single agenda line."""
    if occurrence.all_day:
        when = f"{occurrence.dtstart} (all day)"
    else:
        local = occurrence.dtstart.astimezone()
        when = local.strftime("%Y-%m-%d %H:%M")
    location = f" @ {occurrence.event.location}" if occurrence.event.location else ""
    return f"{when}  {occurrence.event.summary}{location}"


def _occurrence_to_dict(occurrence: Occurrence) -> dict:
    """Render one occurrence as a JSON-friendly dictionary."""
    return {
        "uid": occurrence.event.uid,
        "calendar_uid": occurrence.event.calendar_uid,
        "summary": occurrence.event.summary,
        "location": occurrence.event.location,
        "all_day": occurrence.all_day,
        "dtstart": occurrence.dtstart.isoformat(),
        "dtend": occurrence.dtend.isoformat(),
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """CalDAV Cal API: read and search CalDAV calendars from the terminal."""


@cli.command("list-calendars")
@connection_options
def list_calendars(url, username, password, nextcloud_mode, target_calendars, debug):
    """List the calendars available on the server, as JSON."""
    if debug:
        enable_debug_logging()
    try:
        api = get_api(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            target_calendars=target_calendars,
            debug=debug,
        )
        # Calendar metadata comes from the connection itself, so no event fetch is
        # needed here: listing calendars must stay fast even on huge accounts.
        payload = [
            {"name": str(cal.name), "uid": str(cal.id)} for cal in api.raw_calendars
        ]
        click.echo(json.dumps(payload, indent=2))
        if debug:
            _drop_into_console(locals())
    except click.UsageError as ue:
        click.echo(f"Configuration error: {ue}", err=True)
        raise click.Abort()
    except ConnectionError as ce:
        click.echo(f"Connection failed: {ce}", err=True)
        raise click.Abort()
    except Exception as e:
        logger.exception("list-calendars failed")
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("list-upcoming")
@connection_options
@click.option(
    "--calendar-uid",
    default="",
    envvar="CALDAV_CAL_API_DEFAULT_CALENDAR_UID",
    help="Restrict to one calendar UID.",
)
@click.option("--days", default=7, show_default=True, help="How far ahead to look.")
@click.option("--limit", default=25, show_default=True, help="Maximum events to show.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def list_upcoming(
    url,
    username,
    password,
    nextcloud_mode,
    target_calendars,
    debug,
    calendar_uid,
    days,
    limit,
    as_json,
):
    """Show the next events, with recurring events expanded into occurrences."""
    if debug:
        enable_debug_logging()
    try:
        now = datetime.datetime.now(UTC)
        end = now + datetime.timedelta(days=days)
        api = get_api(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            target_calendars=target_calendars,
            debug=debug,
            # The load window is widened backwards so that a recurring event whose
            # master started long ago, or an event already in progress, is not missed.
            window_start=-365,
            window_end=days + 1,
        )
        api.load_remote_data()
        occurrences = api.get_occurrences_in_range(now, end, calendar_uid=calendar_uid)[
            :limit
        ]

        if as_json:
            click.echo(
                json.dumps([_occurrence_to_dict(o) for o in occurrences], indent=2)
            )
        elif not occurrences:
            click.echo(f"No events in the next {days} day(s).")
        else:
            for occurrence in occurrences:
                click.echo(_format_occurrence(occurrence))
        if debug:
            _drop_into_console(locals())
    except click.UsageError as ue:
        click.echo(f"Configuration error: {ue}", err=True)
        raise click.Abort()
    except ConnectionError as ce:
        click.echo(f"Connection failed: {ce}", err=True)
        raise click.Abort()
    except Exception as e:
        logger.exception("list-upcoming failed")
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("search")
@click.argument("text")
@connection_options
@click.option(
    "--calendar-uid",
    default="",
    envvar="CALDAV_CAL_API_DEFAULT_CALENDAR_UID",
    help="Restrict to one calendar UID.",
)
@click.option(
    "--start",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Start of the range to search (YYYY-MM-DD). Overrides the default window.",
)
@click.option(
    "--end",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="End of the range to search (YYYY-MM-DD). Overrides the default window.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def search(
    text,
    url,
    username,
    password,
    nextcloud_mode,
    target_calendars,
    debug,
    calendar_uid,
    start,
    end,
    as_json,
):
    """Find events whose summary, description or location contains TEXT.

    The optional range is applied server-side as the load window; the text match is then
    applied locally, so a narrow range is what makes a search over a large account fast.
    """
    if debug:
        enable_debug_logging()
    try:
        window_start = start.replace(tzinfo=UTC) if start else None
        window_end = end.replace(tzinfo=UTC) if end else None
        api = get_api(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            target_calendars=target_calendars,
            debug=debug,
            window_start=window_start,
            window_end=window_end,
        )
        api.load_remote_data()

        if calendar_uid:
            events = api.get_events_by_calendar_uid(calendar_uid)
        else:
            events = [event for cal in api.calendars for event in cal.events]

        needle = text.lower()
        matches = [
            event
            for event in events
            if needle in event.summary.lower()
            or needle in event.description.lower()
            or needle in event.location.lower()
        ]
        matches.sort(key=lambda e: (e.dtstart is None, str(e.dtstart)))

        if as_json:
            click.echo(json.dumps([event.to_dict() for event in matches], indent=2))
        elif not matches:
            click.echo(f"No event matching '{text}' in the searched range.")
        else:
            for event in matches:
                when = event.dtstart_local if event.dtstart else "undated"
                click.echo(f"{when}  {event.summary}  [uid={event.uid}]")
        if debug:
            _drop_into_console(locals())
    except click.UsageError as ue:
        click.echo(f"Configuration error: {ue}", err=True)
        raise click.Abort()
    except ConnectionError as ce:
        click.echo(f"Connection failed: {ce}", err=True)
        raise click.Abort()
    except Exception as e:
        logger.exception("search failed")
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@cli.command("dump")
@connection_options
@click.option(
    "--calendar-uid",
    default="",
    envvar="CALDAV_CAL_API_DEFAULT_CALENDAR_UID",
    help="Restrict to one calendar UID.",
)
def dump(
    url, username, password, nextcloud_mode, target_calendars, debug, calendar_uid
):
    """Print the raw VEVENT of every loaded event.

    Useful for diffing a calendar into version control, and for filing a bug report about
    an event this library mishandles.
    """
    if debug:
        enable_debug_logging()
    try:
        api = get_api(
            url=url,
            username=username,
            password=password,
            nextcloud_mode=nextcloud_mode,
            target_calendars=target_calendars,
            debug=debug,
        )
        api.load_remote_data()

        calendars = (
            [api.get_calendar_by_uid(calendar_uid)] if calendar_uid else api.calendars
        )
        for calendar in calendars:
            if calendar is None:
                click.echo(f"# No calendar with UID '{calendar_uid}'", err=True)
                continue
            click.echo(f"# Calendar: {calendar.name} (uid={calendar.uid})")
            for event in calendar.events:
                click.echo(f"# Event uid={event.uid}")
                click.echo(event.to_ical())
        if debug:
            _drop_into_console(locals())
    except click.UsageError as ue:
        click.echo(f"Configuration error: {ue}", err=True)
        raise click.Abort()
    except ConnectionError as ce:
        click.echo(f"Connection failed: {ce}", err=True)
        raise click.Abort()
    except Exception as e:
        logger.exception("dump failed")
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
