"""
CalDAV calendar client.

Exposes :class:`CalendarAPI`, which connects to a CalDAV server, loads calendars and
their events into plain Python objects, and writes changes back.

The design mirrors the sibling caldav_tasks_api project (which covers VTODO tasks) so
the two can be used side by side: eager connection in ``__init__``, an in-memory cache
rebuilt by ``load_remote_data()``, a ``read_only`` guard raising ``PermissionError``, and
a ``debug`` flag dropping into ``pdb`` on unexpected failures.

One difference matters enough to state up front: **the cache holds a time window, not the
whole calendar.** A calendar can hold decades of events, so ``load_remote_data()`` fetches
a bounded range (by default the last 30 days and the next year). Anything outside it is
simply absent, and ``get_event_by_global_uid()`` will return ``None`` for an event that
really exists. Pass ``fetch_all=True`` when you genuinely need everything.
"""

import datetime
import os
import pdb
import re
from typing import Optional

import caldav
import urllib3
from caldav import Calendar, DAVClient, Event, Principal
from caldav.elements import dav
from icalendar import Calendar as IcsCalendar
from loguru import logger

from caldav_cal_api.utils.data import CalendarData, EventData, Occurrence

UTC = datetime.timezone.utc

# Defaults for the load window, in days relative to "now". A year forward catches annual
# events and most planning horizons; a month back keeps recent history available for
# reporting without dragging in years of dead weight.
DEFAULT_WINDOW_START_DAYS = -30
DEFAULT_WINDOW_END_DAYS = 365

# Apache's mod_deflate and mod_brotli append a suffix to the ETag of any response they
# compress, so the same resource is advertised as '"abc"' uncompressed and '"abc-gzip"'
# compressed. Echoing the suffixed form back in If-Match makes the server compare it
# against the stored '"abc"' and answer 412. See _normalize_etag.
_ETAG_ENCODING_SUFFIX = re.compile(r'^(W/)?"(.*?)(?:-(?:gzip|br|deflate))"$')


class CalendarAPI:
    """Client for reading and writing CalDAV calendar events.

    Parameters
    ----------
    url : str, optional
        CalDAV server URL. Falls back to ``CALDAV_CAL_API_URL``.
    username : str, optional
        Falls back to ``CALDAV_CAL_API_USERNAME``.
    password : str, optional
        Falls back to ``CALDAV_CAL_API_PASSWORD``. Never stored on the instance.
    nextcloud_mode : bool, optional
        Append Nextcloud's ``remote.php/dav/`` path to `url` when it is missing.
    debug : bool, optional
        Drop into ``pdb.post_mortem()`` on unexpected failures.
    target_calendars : list of str, optional
        Restrict loading to calendars matching these ids or names. Loading fewer
        calendars is the single most effective way to speed up ``load_remote_data()``.
    read_only : bool, optional
        Refuse every write with ``PermissionError``. Useful as a dry-run switch.
    ssl_verify_cert : bool, optional
        Verify the server's TLS certificate.
    window_start : int or datetime.datetime, optional
        Start of the load window. An int is a day offset from now (negative for the
        past). Falls back to ``CALDAV_CAL_API_WINDOW_START_DAYS``, then to -30.
    window_end : int or datetime.datetime, optional
        End of the load window, same conventions. Default +365 days.
    fetch_all : bool, optional
        Ignore the window and fetch every event. Falls back to
        ``CALDAV_CAL_API_FETCH_ALL``. Can be very slow on long-lived calendars.

    Raises
    ------
    ValueError
        If the URL, username or password is missing.
    ConnectionError
        If the server cannot be reached or authentication fails.
    """

    VERSION: str = "0.1.0"

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
        window_start: Optional[int | datetime.datetime] = None,
        window_end: Optional[int | datetime.datetime] = None,
        fetch_all: bool = False,
    ):
        self.url = url or os.environ.get("CALDAV_CAL_API_URL")
        self.username = username or os.environ.get("CALDAV_CAL_API_USERNAME")
        # The password is kept in a local only, never as an attribute: an accidental
        # repr() or log dump of the API object must not leak it.
        password_for_connection = password or os.environ.get("CALDAV_CAL_API_PASSWORD")

        if not self.url:
            raise ValueError(
                "CalDAV URL is required. Pass url= or set CALDAV_CAL_API_URL."
            )
        if not self.username:
            raise ValueError(
                "CalDAV username is required. Pass username= or set "
                "CALDAV_CAL_API_USERNAME."
            )
        if not password_for_connection:
            raise ValueError(
                "CalDAV password is required. Pass password= or set "
                "CALDAV_CAL_API_PASSWORD."
            )

        self.nextcloud_mode = nextcloud_mode
        self.debug = debug
        self.target_calendars = target_calendars
        self.read_only = read_only
        self.ssl_verify_cert = ssl_verify_cert
        self.window_start = window_start
        self.window_end = window_end
        self.fetch_all = fetch_all or _env_flag("CALDAV_CAL_API_FETCH_ALL")

        logger.debug(
            f"CalendarAPI initializing with URL: {self.url}, User: {self.username}, "
            f"Nextcloud mode: {self.nextcloud_mode}, Read-only: {self.read_only}, "
            f"Fetch all: {self.fetch_all}, Target calendars: {self.target_calendars}"
        )

        self._adjust_url()

        self.client: Optional[DAVClient] = None
        self.principal: Optional[Principal] = None
        self.raw_calendars: list[Calendar] = []  # caldav.Calendar objects
        self.calendars: list[CalendarData] = []  # Our own objects, holding the events

        self._connect(password=password_for_connection)

    # --- Connection -------------------------------------------------------------

    def _adjust_url(self) -> None:
        """Normalize the server URL, adding a scheme and the Nextcloud DAV path.

        Notes
        -----
        Users copy the URL out of their browser's address bar, which for Nextcloud is the
        web UI root rather than the DAV endpoint. Fixing that here saves a confusing
        authentication failure.
        """
        original_url = self.url

        if not self.url.startswith(("http://", "https://")):
            self.url = f"https://{self.url}"

        if self.nextcloud_mode and "remote.php/dav" not in self.url:
            separator = "" if self.url.endswith("/") else "/"
            self.url = f"{self.url}{separator}remote.php/dav/"

        if self.url != original_url:
            logger.info(f"Adjusted CalDAV URL from '{original_url}' to '{self.url}'.")
        else:
            logger.debug(f"CalDAV URL used as-is: '{self.url}'.")

    def _connect(self, password: str) -> None:
        """Open the DAV connection and fetch the calendar list.

        Raises
        ------
        ConnectionError
            Wrapping any underlying failure, so callers have a single exception type to
            catch regardless of which library layer failed.
        """
        try:
            if not self.ssl_verify_cert:
                # Only silence the warning when the user explicitly opted out, otherwise
                # a genuine certificate problem would be hidden.
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            self.client = DAVClient(
                url=self.url,
                username=self.username,
                password=password,
                ssl_verify_cert=self.ssl_verify_cert,
            )
            self.principal = self.client.principal()
            logger.debug(f"Connected to CalDAV server as '{self.username}'.")
            self._fetch_raw_calendars()
        except Exception as e:
            logger.error(f"Failed to connect to CalDAV server: {e}")
            raise ConnectionError(f"Failed to connect to CalDAV server: {e}")

    def _fetch_raw_calendars(self) -> None:
        """Populate ``self.raw_calendars`` with the calendars that hold events."""
        try:
            all_calendars = self.principal.calendars()
            self.raw_calendars = [
                cal
                for cal in all_calendars
                if (
                    self.target_calendars is None
                    or str(cal.id) in self.target_calendars
                    or str(cal.name) in self.target_calendars
                )
                # A CalDAV collection may hold tasks only; asking it for events would
                # either error or waste a round trip.
                and "VEVENT" in cal.get_supported_components()
            ]
            logger.debug(
                f"Found {len(self.raw_calendars)} calendar(s) supporting VEVENT."
            )
        except Exception as e:
            logger.error(f"Failed to fetch calendars: {e}")
            self.raw_calendars = []

    # --- Loading ----------------------------------------------------------------

    def _resolve_window(
        self,
        window_start: Optional[int | datetime.datetime],
        window_end: Optional[int | datetime.datetime],
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """Resolve the load window to a concrete pair of UTC datetimes.

        Notes
        -----
        Resolution order is method argument, then constructor argument, then env var,
        then the module default. Day offsets are resolved against *now* at call time
        rather than at construction, so a long-lived instance re-slides its window on
        every reload instead of drifting into the past.
        """
        now = datetime.datetime.now(UTC)

        def resolve(
            value: Optional[int | datetime.datetime], env_var: str, default_days: int
        ) -> datetime.datetime:
            if isinstance(value, datetime.datetime):
                return value if value.tzinfo else value.replace(tzinfo=UTC)
            days = value
            if days is None:
                raw = os.environ.get(env_var)
                if raw is not None:
                    try:
                        days = int(raw)
                    except ValueError:
                        logger.warning(
                            f"Ignoring non-integer {env_var}='{raw}'; "
                            f"using {default_days}."
                        )
            if days is None:
                days = default_days
            return now + datetime.timedelta(days=days)

        start = resolve(
            window_start if window_start is not None else self.window_start,
            "CALDAV_CAL_API_WINDOW_START_DAYS",
            DEFAULT_WINDOW_START_DAYS,
        )
        end = resolve(
            window_end if window_end is not None else self.window_end,
            "CALDAV_CAL_API_WINDOW_END_DAYS",
            DEFAULT_WINDOW_END_DAYS,
        )
        return start, end

    def load_remote_data(
        self,
        *,
        window_start: Optional[int | datetime.datetime] = None,
        window_end: Optional[int | datetime.datetime] = None,
        fetch_all: Optional[bool] = None,
    ) -> None:
        """Load calendars and their events into memory, replacing any previous cache.

        Parameters
        ----------
        window_start, window_end : int or datetime.datetime, optional
            Override the constructor's window for this call. Ints are day offsets.
        fetch_all : bool, optional
            Override the constructor's ``fetch_all`` for this call.

        Raises
        ------
        ConnectionError
            If the client is not connected.

        Notes
        -----
        Events are fetched with server-side expansion turned off. Expansion would return
        one component per occurrence, each carrying a RECURRENCE-ID, which is exactly the
        model this library declined, and it would destroy the RRULE that
        :meth:`EventData.get_occurrences` needs.

        A recurring event whose DTSTART is older than the window still shows up as long
        as one of its occurrences falls inside it, because RFC 4791 requires servers to
        match time ranges against expanded occurrences. Sabre-based servers (Nextcloud,
        ownCloud) do this correctly.
        """
        if not self.principal:
            raise ConnectionError("Not connected to CalDAV server.")

        use_fetch_all = self.fetch_all if fetch_all is None else fetch_all
        start, end = self._resolve_window(window_start, window_end)

        self._fetch_raw_calendars()
        self.calendars = []

        total_events = 0
        for raw_calendar in self.raw_calendars:
            calendar_data = CalendarData(
                uid=str(raw_calendar.id),
                name=raw_calendar.name if raw_calendar.name else "Unnamed Calendar",
                color=_read_calendar_color(raw_calendar),
                synced=True,
            )
            self.calendars.append(calendar_data)

            raw_events = self._fetch_raw_events(
                raw_calendar=raw_calendar,
                start=start,
                end=end,
                fetch_all=use_fetch_all,
            )

            failed = 0
            for raw_event in raw_events:
                try:
                    event = EventData.from_ical(
                        raw_event.data, calendar_uid=calendar_data.uid
                    )
                    event.synced = True
                    event._api_reference = self
                    event._href = str(raw_event.url) if raw_event.url else ""
                    calendar_data.events.append(event)
                except Exception as e:
                    # One malformed event must never abort a whole calendar.
                    failed += 1
                    logger.warning(
                        f"Skipping an unparseable event in calendar "
                        f"'{calendar_data.name}': {e}"
                    )

            total_events += len(calendar_data.events)
            logger.debug(
                f"Calendar '{calendar_data.name}': {len(calendar_data.events)} event(s) "
                f"loaded, {failed} skipped."
            )

        window_description = (
            "all events" if use_fetch_all else f"{start.date()} to {end.date()}"
        )
        logger.info(
            f"Finished loading remote data ({window_description}). "
            f"Calendars: {len(self.calendars)}, events: {total_events}."
        )

    def _fetch_raw_events(
        self,
        *,
        raw_calendar: Calendar,
        start: datetime.datetime,
        end: datetime.datetime,
        fetch_all: bool,
    ) -> list[Event]:
        """Fetch caldav Event objects for one calendar, with a raw-parsing fallback.

        Notes
        -----
        The fallback re-reads the collection's own ICS blob and walks it with icalendar.
        Some servers fail a structured search but happily serve the raw data, and the
        sibling project hit exactly that, so the escape hatch is worth its few lines.
        """
        try:
            if fetch_all:
                return list(raw_calendar.events())
            return list(
                raw_calendar.search(start=start, end=end, event=True, expand=False)
            )
        except Exception as e:
            logger.warning(
                f"Structured event fetch failed for calendar '{raw_calendar.name}' "
                f"({e}). Falling back to parsing the raw collection data."
            )

        try:
            parsed = IcsCalendar.from_ical(raw_calendar.data)
            return [_RawEventShim(component) for component in parsed.walk("VEVENT")]
        except Exception as e:
            logger.error(
                f"Raw fallback also failed for calendar '{raw_calendar.name}': {e}"
            )
            if self.debug:
                pdb.post_mortem()
            return []

    # --- Lookups ----------------------------------------------------------------

    def get_calendar_by_uid(self, uid: str) -> Optional[CalendarData]:
        """Return the loaded calendar with this UID, or None."""
        for calendar in self.calendars:
            if calendar.uid == uid:
                return calendar
        return None

    def get_events_by_calendar_uid(self, calendar_uid: str) -> list[EventData]:
        """Return the loaded events of one calendar, or an empty list if unknown."""
        calendar = self.get_calendar_by_uid(calendar_uid)
        return calendar.events if calendar else []

    def get_event_by_global_uid(self, uid: str) -> Optional[EventData]:
        """Find an event by UID across every loaded calendar.

        Returns
        -------
        EventData or None
            ``None`` also when the event exists on the server but falls outside the
            loaded window. See the module docstring.
        """
        for calendar in self.calendars:
            for event in calendar.events:
                if event.uid == uid:
                    return event
        return None

    def get_occurrences_in_range(
        self,
        start: datetime.datetime | datetime.date,
        end: datetime.datetime | datetime.date,
        *,
        calendar_uid: str = "",
    ) -> list[Occurrence]:
        """Expand every loaded event into occurrences overlapping ``[start, end)``.

        Parameters
        ----------
        start, end : datetime.datetime or datetime.date
            The window to expand into.
        calendar_uid : str, optional
            Restrict to a single calendar. Empty means all loaded calendars.

        Returns
        -------
        list of Occurrence
            Sorted by start time.

        Notes
        -----
        Expansion is purely local, over what ``load_remote_data()`` already fetched, so a
        range extending past the load window will simply have nothing to expand there.
        """
        if calendar_uid:
            events = self.get_events_by_calendar_uid(calendar_uid)
        else:
            events = [event for calendar in self.calendars for event in calendar.events]

        occurrences: list[Occurrence] = []
        for event in events:
            occurrences.extend(event.get_occurrences(start, end))

        occurrences.sort(
            key=lambda occ: (
                occ.dtstart
                if isinstance(occ.dtstart, datetime.datetime)
                else datetime.datetime.combine(
                    occ.dtstart, datetime.time.min, tzinfo=UTC
                )
            )
        )
        return occurrences

    # --- Writes -----------------------------------------------------------------

    def _require_writable(self, action: str) -> None:
        """Raise if the API was opened read-only."""
        if self.read_only:
            raise PermissionError(f"API is in read-only mode. {action} is not allowed.")

    def _resolve_calendar_uid(self, calendar_uid: Optional[str], fallback: str) -> str:
        """Settle which calendar to act on, falling back to an env default."""
        for candidate in (
            calendar_uid,
            fallback,
            os.environ.get("CALDAV_CAL_API_DEFAULT_CALENDAR_UID"),
        ):
            if candidate:
                return candidate
        raise ValueError(
            "No calendar UID given. Pass calendar_uid=, set it on the event, or set "
            "CALDAV_CAL_API_DEFAULT_CALENDAR_UID."
        )

    def _get_raw_calendar(self, calendar_uid: str) -> Calendar:
        """Return the caldav.Calendar for a UID.

        Raises
        ------
        ValueError
            If no connected calendar has this UID.
        """
        if not self.raw_calendars:
            self._fetch_raw_calendars()
        for raw_calendar in self.raw_calendars:
            if str(raw_calendar.id) == calendar_uid:
                return raw_calendar
        raise ValueError(
            f"No calendar with UID '{calendar_uid}' is available on this connection."
        )

    def _get_server_event(
        self, raw_calendar: Calendar, event_uid: str, href: str
    ) -> Event:
        """Fetch the server-side object for an event.

        Notes
        -----
        The href recorded at load time is tried first because addressing the object
        directly avoids a REPORT query. It is only a shortcut: hrefs are not always
        ``<uid>.ics``, and a stale one falls back to a UID lookup.
        """
        if href:
            try:
                server_event = Event(client=self.client, url=href, parent=raw_calendar)
                server_event.load()
                _normalize_etag(server_event)
                return server_event
            except Exception as e:
                logger.debug(
                    f"Direct href lookup failed for '{href}' ({e}); "
                    "falling back to a UID search."
                )
        server_event = raw_calendar.event_by_uid(event_uid)
        _normalize_etag(server_event)
        return server_event

    def add_event(
        self, event: EventData, calendar_uid: Optional[str] = None
    ) -> EventData:
        """Create an event on the server.

        Parameters
        ----------
        event : EventData
            The event to create. Its ``calendar_uid`` is used when `calendar_uid` is not
            given.
        calendar_uid : str, optional
            Target calendar, overriding the event's own.

        Returns
        -------
        EventData
            The same object, updated with the server's view and marked synced.

        Raises
        ------
        PermissionError
            If the API is read-only.
        ValueError
            If no target calendar can be determined, or it is unknown.
        """
        self._require_writable("Adding events")

        target_uid = self._resolve_calendar_uid(calendar_uid, event.calendar_uid)
        raw_calendar = self._get_raw_calendar(target_uid)
        event.calendar_uid = target_uid

        try:
            created = raw_calendar.save_event(event.to_vcalendar())
            # Re-read the server's copy: it is authoritative for the UID and for any
            # property the server rewrote or filled in.
            stored = EventData.from_ical(created.data, calendar_uid=target_uid)
            event.uid = stored.uid or event.uid
            event.changed_at = stored.changed_at or event.changed_at
            event.sequence = stored.sequence
            event._raw_component = stored._raw_component
            event._href = str(created.url) if created.url else ""
            event._api_reference = self
            event.synced = True
            logger.info(
                f"Created event '{event.summary}' (uid={event.uid}) in calendar "
                f"'{target_uid}'."
            )
            return event
        except Exception as e:
            logger.error(f"Failed to add event '{event.summary}': {e}")
            event.synced = False
            if self.debug:
                pdb.post_mortem()
            raise

    def update_event(self, event: EventData) -> EventData:
        """Write local changes back to the server.

        Parameters
        ----------
        event : EventData
            An event carrying both a UID and a calendar UID.

        Returns
        -------
        EventData
            The same object, refreshed from the server and marked synced.

        Raises
        ------
        PermissionError
            If the API is read-only.
        ValueError
            If the event lacks a UID or calendar UID, or is not found on the server.

        Notes
        -----
        SEQUENCE is incremented on every update. Other CalDAV clients are entitled to
        ignore a revision whose SEQUENCE did not advance, so skipping this would make
        edits silently invisible to attendees.
        """
        self._require_writable("Updating events")

        if not event.uid:
            raise ValueError("Cannot update an event without a UID.")
        if not event.calendar_uid:
            raise ValueError(
                f"Event '{event.uid}' has no calendar_uid, so its calendar is unknown."
            )

        raw_calendar = self._get_raw_calendar(event.calendar_uid)

        event.sequence += 1
        event.changed_at = datetime.datetime.now(UTC).replace(microsecond=0)

        try:
            server_event = self._get_server_event(
                raw_calendar=raw_calendar, event_uid=event.uid, href=event._href
            )
            server_event.data = event.to_vcalendar()
            server_event.save()

            refreshed = EventData.from_ical(
                server_event.data, calendar_uid=event.calendar_uid
            )
            event._raw_component = refreshed._raw_component
            event.changed_at = refreshed.changed_at or event.changed_at
            event.sequence = refreshed.sequence or event.sequence
            event._api_reference = self
            event.synced = True
            logger.info(f"Updated event '{event.summary}' (uid={event.uid}).")
            return event
        except caldav.lib.error.NotFoundError:
            raise ValueError(
                f"Event with UID '{event.uid}' not found in calendar "
                f"'{event.calendar_uid}'."
            )
        except Exception as e:
            logger.error(f"Failed to update event '{event.uid}': {e}")
            event.synced = False
            if self.debug:
                pdb.post_mortem()
            raise

    def delete_event_by_id(self, uid: str, calendar_uid: Optional[str] = None) -> bool:
        """Delete an event from the server.

        Parameters
        ----------
        uid : str
            UID of the event to delete.
        calendar_uid : str, optional
            Calendar holding it. Falls back to ``CALDAV_CAL_API_DEFAULT_CALENDAR_UID``.

        Returns
        -------
        bool
            True when the server accepted the deletion.

        Raises
        ------
        PermissionError
            If the API is read-only.
        ValueError
            If the calendar cannot be determined, or the event is not found.
        """
        self._require_writable("Deleting events")

        target_uid = self._resolve_calendar_uid(calendar_uid, "")
        raw_calendar = self._get_raw_calendar(target_uid)

        try:
            server_event = raw_calendar.event_by_uid(uid)
            # Logged before deleting so the content stays recoverable from the log.
            logger.info(f"Deleting event uid='{uid}' from calendar '{target_uid}'.")
            logger.debug(f"Deleted event content:\n{server_event.data}")
            server_event.delete()
            return True
        except caldav.lib.error.NotFoundError:
            raise ValueError(
                f"Event with UID '{uid}' not found in calendar '{target_uid}' "
                "for deletion."
            )
        except Exception as e:
            logger.error(f"Failed to delete event '{uid}': {e}")
            if self.debug:
                pdb.post_mortem()
            raise


def _env_flag(name: str) -> bool:
    """Read a boolean env var, accepting the usual truthy spellings."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_etag(server_event: Event) -> None:
    """Strip any content-encoding suffix from a freshly loaded event's ETag.

    Notes
    -----
    ``caldav`` sends the ETag it holds as an ``If-Match`` header on every write, which is
    the optimistic concurrency check we want. Apache appends ``-gzip`` (or ``-br``) to the
    ETag of responses it compresses, though, so a GET large enough to be compressed hands
    back a token the server will not recognise on the way back in: the write then fails
    with 412 Precondition Failed.

    Size is what decides it, which is why this surfaced only on the bigger events. A
    ``DTSTART;TZID=`` forces a VTIMEZONE into the payload and pushes it past the
    compression threshold, so timed events in a named zone hit it while plain UTC ones
    slip under. Rewriting the ETag in place keeps If-Match working rather than dropping
    the concurrency check.
    """
    etag = server_event.props.get(dav.GetEtag.tag)
    if not etag:
        return
    match = _ETAG_ENCODING_SUFFIX.match(etag)
    if match:
        cleaned = f'{match.group(1) or ""}"{match.group(2)}"'
        logger.debug(f"Normalized ETag {etag} to {cleaned} for the If-Match header.")
        server_event.props[dav.GetEtag.tag] = cleaned


def _read_calendar_color(raw_calendar: Calendar) -> str:
    """Best-effort read of a calendar's color.

    Notes
    -----
    Not every server exposes this property, and it is cosmetic, so a failure is silently
    treated as "no color". The sibling project declares a color field it never populates;
    this at least fills it in where the server cooperates.
    """
    try:
        color = raw_calendar.get_property(caldav.elements.ical.CalendarColor())
        return str(color) if color else ""
    except Exception:
        return ""


class _RawEventShim:
    """Minimal stand-in for a caldav.Event, used by the raw-parsing fallback.

    Notes
    -----
    The fallback path parses a collection's ICS blob directly, which yields icalendar
    components rather than caldav resources. Wrapping them in something exposing ``.data``
    and ``.url`` lets the caller treat both paths identically instead of branching.
    """

    def __init__(self, component):
        self.data = component.to_ical().decode("utf-8", errors="replace")
        self.url = None
