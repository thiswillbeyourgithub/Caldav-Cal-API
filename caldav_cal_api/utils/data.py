"""
Data structures for caldav-cal-api.

Defines the plain-dataclass objects the library exposes:

- :class:`CalendarData`: a calendar (a CalDAV collection) and the events loaded from it.
- :class:`EventData`: a single VEVENT master component.
- :class:`Occurrence`: one expanded instance of a recurring event.
- :class:`XProperties`: attribute-style access to custom ``X-`` properties.

Two conventions govern this module and are worth knowing before reading further:

1. **Datetimes are timezone-aware and normalized to UTC**, while the original ``TZID`` is
   kept alongside in a companion field (``dtstart_tzid`` / ``dtend_tzid``). Keeping the
   value a real ``datetime`` means comparison, ``sorted()`` and dateutil all work without
   unwrapping; keeping the TZID means writes round-trip into the user's zone and
   recurrence expansion can happen in the zone the rule was written for.
2. **All-day events use ``datetime.date``, not midnight datetimes.** An all-day event has
   no instant, and forcing one makes every consumer re-derive the date and get it wrong
   one timezone over. ``all_day`` is a mirror of ``dtstart``'s type, reconciled in
   ``__post_init__``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional, TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import icalendar
from loguru import logger

if TYPE_CHECKING:
    from caldav_cal_api.caldav_cal_api import CalendarAPI

UTC = datetime.timezone.utc


def _resolve_zone(tzid: str) -> Optional[ZoneInfo]:
    """Turn a TZID string into a :class:`zoneinfo.ZoneInfo`, or ``None`` if unusable.

    Parameters
    ----------
    tzid : str
        An IANA timezone name, e.g. ``"Europe/Paris"``. May be empty.

    Returns
    -------
    zoneinfo.ZoneInfo or None
        ``None`` when `tzid` is empty or does not name a zone this system knows.

    Notes
    -----
    An unknown TZID is logged and swallowed rather than raised. Servers occasionally
    emit non-IANA zone names (Windows-style ones, or entries defined only in the file's
    own VTIMEZONE block), and one exotic event must not abort a whole calendar load.
    """
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except Exception as e:  # ZoneInfoNotFoundError, but also ValueError on odd input
        logger.warning(f"Unusable TZID '{tzid}' ({e}). Treating the value as UTC.")
        return None


def _to_utc_aware(
    value: Optional[datetime.datetime],
    tzid: str,
    field_name: str,
) -> tuple[Optional[datetime.datetime], str]:
    """Normalize a datetime to an aware UTC value and settle its companion TZID.

    Parameters
    ----------
    value : datetime.datetime or None
        The datetime to normalize. ``None`` passes through untouched.
    tzid : str
        The TZID that accompanied the value, possibly empty.
    field_name : str
        Field name, used only to make log messages identifiable.

    Returns
    -------
    tuple of (datetime.datetime or None, str)
        The UTC-normalized datetime and the TZID that should be stored with it.

    Notes
    -----
    Three inputs are possible and all three are handled here rather than at the call
    sites, so that hand-built objects and parsed ones end up in the same shape:

    - naive with a TZID: interpreted in that zone;
    - naive with no TZID: a floating time. RFC 5545 says it means "local time wherever
      it is read", which cannot be represented as an instant, so it is assumed to be UTC
      and a warning is emitted rather than silently guessing;
    - aware: converted to UTC, and if no TZID was supplied the incoming ``tzinfo``'s IANA
      key is captured so the original zone is not lost.
    """
    if value is None:
        return None, ""

    zone = _resolve_zone(tzid)
    if tzid and zone is None:
        tzid = (
            ""  # Do not keep a TZID we cannot resolve; it would break round-tripping.
        )

    if value.tzinfo is None:
        if zone is not None:
            value = value.replace(tzinfo=zone)
        else:
            logger.warning(
                f"{field_name} is a floating time (naive, no TZID). Assuming UTC."
            )
            value = value.replace(tzinfo=UTC)
    elif not tzid:
        # ZoneInfo instances expose the IANA name as .key; other tzinfo implementations
        # (pytz, dateutil.tz, plain timezone offsets) do not, in which case there is
        # nothing meaningful to preserve and "" correctly means "UTC or floating".
        key = getattr(value.tzinfo, "key", "")
        if key and key != "UTC":
            tzid = key

    return value.astimezone(UTC), tzid


class XProperties:
    """Wrapper around custom ``X-`` properties allowing attribute-style access.

    Raw keys (including any parameters) are preserved exactly as the server sent them,
    while lookups accept a normalized form: ``X-APPLE-SORT-ORDER`` is reachable as
    ``x_properties.apple_sort_order``.

    Parameters
    ----------
    initial_data : dict of str to str, optional
        Raw property keys mapped to their values.

    Notes
    -----
    Ported verbatim from the sibling caldav_tasks_api project so X-property handling is
    identical across the two libraries.
    """

    def __init__(self, initial_data: Optional[Dict[str, str]] = None):
        self._raw_properties: Dict[str, str] = (
            initial_data if initial_data is not None else {}
        )

    def __getattr__(self, name: str) -> str:
        """Look up a property by its normalized name.

        The raw key is lowercased, stripped of any parameters and of the ``x-`` prefix,
        and has hyphens turned into underscores before comparison.
        """
        normalized_name_query = name.lower()

        for raw_key, raw_value in self._raw_properties.items():
            # "X-APPLE-SORT-ORDER;FOO=BAR" -> "apple_sort_order"
            key_for_comparison = raw_key.split(";")[0].lower()
            if key_for_comparison.startswith("x-"):
                key_for_comparison = key_for_comparison[2:]
            key_for_comparison = key_for_comparison.replace("-", "_")

            if key_for_comparison == normalized_name_query:
                return raw_value

        raise AttributeError(
            f"'{type(self).__name__}' object has no X-property corresponding to attribute '{name}'. "
            f"Searched for normalized form '{normalized_name_query}'. "
            f"Available raw X-property keys: {list(self._raw_properties.keys())}"
        )

    def __setitem__(self, key: str, value: str) -> None:
        """Store a property under its original key."""
        self._raw_properties[key] = value

    def __getitem__(self, key: str) -> str:
        """Retrieve a property by raw key, falling back to a case-insensitive match."""
        try:
            return self._raw_properties[key]
        except KeyError:
            key_lower = key.lower()
            for raw_key, value in self._raw_properties.items():
                if raw_key.lower() == key_lower:
                    return value
            raise KeyError(key)

    def get_raw_properties(self) -> Dict[str, str]:
        """Return the underlying dictionary of raw properties."""
        return self._raw_properties

    def items(self):
        """Iterate over ``(raw_key, value)`` pairs, like a dictionary."""
        return self._raw_properties.items()

    def __contains__(self, key: str) -> bool:
        """Case-insensitive membership test.

        Notes
        -----
        Beyond the plain and case-insensitive comparisons, this also matches keys whose
        prefix is identical and whose remainder differs only in case. Servers routinely
        normalize the case of an embedded UUID, which would otherwise make a property
        written by this library unfindable on read-back.
        """
        if key in self._raw_properties:
            return True

        key_lower = key.lower()
        for raw_key in self._raw_properties:
            if raw_key.lower() == key_lower:
                return True

        parts = key.split("-", 2)  # e.g. X-TEST-PROP-uuid -> ["X", "TEST", "PROP-uuid"]
        if len(parts) >= 3:
            prefix = "-".join(parts[0:2])
            uuid_part = parts[2]

            for raw_key in self._raw_properties:
                raw_parts = raw_key.split("-", 2)
                if len(raw_parts) >= 3:
                    raw_prefix = "-".join(raw_parts[0:2])
                    raw_uuid_part = raw_parts[2]

                    if (
                        prefix.lower() == raw_prefix.lower()
                        and uuid_part.lower() == raw_uuid_part.lower()
                    ):
                        return True

        return False

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._raw_properties!r})"

    def __bool__(self) -> bool:
        """Truthy when at least one property is stored."""
        return bool(self._raw_properties)


@dataclass
class CalendarData:
    """A CalDAV calendar collection and the events loaded from it.

    Attributes are ordered alphabetically (with the event list last), matching the house
    style of the sibling caldav_tasks_api project.
    """

    color: str = (
        ""  # APPLE-CALENDAR-COLOR / calendar-color, "" when the server sends none
    )
    deleted: bool = False  # Internal flag, may be used to mark for server-side deletion
    name: str = ""  # displayname
    synced: bool = False  # Internal: whether this mirrors the server as last seen
    uid: str = ""  # Calendar id as reported by caldav
    events: list[EventData] = field(
        default_factory=list
    )  # Events loaded for this calendar

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = str(uuid4())

    def __str__(self) -> str:
        """Return a user-friendly one-line representation."""
        return (
            f"<CalendarData Name: '{self.name}', UID: {self.uid}, "
            f"Events: {len(self.events)}>"
        )

    def __repr__(self) -> str:
        """Return a developer-friendly representation, summarizing events by count."""
        return (
            f"{self.__class__.__name__}("
            f"uid='{self.uid}', name='{self.name}', color='{self.color}', "
            f"deleted={self.deleted}, synced={self.synced}, "
            f"events_count={len(self.events)})"
        )

    def __iter__(self):
        """Iterate over the calendar's events, so ``for event in calendar:`` works."""
        return iter(self.events)

    def __len__(self) -> int:
        """Number of events currently loaded for this calendar."""
        return len(self.events)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a plain JSON-serializable dictionary."""
        return {
            "uid": self.uid,
            "name": self.name,
            "color": self.color,
            "deleted": self.deleted,
            "synced": self.synced,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass
class EventData:
    """A single VEVENT master component.

    Datetimes are timezone-aware and normalized to UTC; the original TZID lives in the
    ``*_tzid`` companion fields so writes round-trip into the user's zone. All-day events
    use :class:`datetime.date` values with ``all_day`` set.

    Notes
    -----
    Only a core set of properties is modeled. Everything else the server sent (ATTENDEE,
    ORGANIZER, VALARM, TRANSP, CLASS, ...) is retained verbatim in ``_raw_component`` and
    re-emitted on write, so updating an event never silently discards data this class
    does not understand. This is a deliberate divergence from the sibling
    caldav_tasks_api, whose TaskData regenerates its VTODO from scratch.
    """

    all_day: bool = False  # DTSTART;VALUE=DATE (mirrors dtstart's type)
    calendar_uid: str = ""  # Owning CalendarData.uid, not an iCal property
    categories: list[str] = field(default_factory=list)  # CATEGORIES
    changed_at: Optional[datetime.datetime] = None  # LAST-MODIFIED (UTC per RFC 5545)
    created_at: Optional[datetime.datetime] = None  # DTSTAMP (UTC per RFC 5545)
    description: str = ""  # DESCRIPTION
    dtend: Optional[datetime.datetime | datetime.date] = None  # DTEND (exclusive bound)
    dtend_tzid: str = ""  # DTEND;TZID=, "" meaning UTC, floating or all-day
    dtstart: Optional[datetime.datetime | datetime.date] = None  # DTSTART
    dtstart_tzid: str = ""  # DTSTART;TZID=, "" meaning UTC, floating or all-day
    exdate: list[str] = field(default_factory=list)  # EXDATE, raw values, one per line
    location: str = ""  # LOCATION
    rdate: list[str] = field(default_factory=list)  # RDATE, raw values, one per line
    rrule: str = ""  # RRULE, raw value e.g. "FREQ=WEEKLY;BYDAY=MO"
    sequence: int = 0  # SEQUENCE
    status: str = ""  # STATUS: TENTATIVE, CONFIRMED or CANCELLED
    summary: str = ""  # SUMMARY
    synced: bool = False  # Internal: set by CalendarAPI only, never inferred from ical
    uid: str = ""  # UID
    x_properties: XProperties = field(default_factory=XProperties)  # X-* properties

    # Private state, excluded from repr and equality so two events compare on their
    # modeled fields alone (mirrors the sibling's _api_reference convention).
    _api_reference: Optional["CalendarAPI"] = field(
        default=None, repr=False, compare=False
    )  # Back-reference to the API, set by CalendarAPI, powers delete()/get_occurrences()
    _href: str = field(
        default="", repr=False, compare=False
    )  # CalDAV object href; not always "<uid>.ics", so it must be remembered, not derived
    _raw_component: Optional[icalendar.Event] = field(
        default=None, repr=False, compare=False
    )  # Source component, so unmodeled properties survive a write
    _source_duration: Optional[datetime.timedelta] = field(
        default=None, repr=False, compare=False
    )  # Set only when the source used DURATION instead of DTEND

    def __post_init__(self) -> None:
        """Reconcile the all-day flag, normalize datetimes and fill in defaults.

        Raises
        ------
        ValueError
            If both ``dtstart`` and ``dtend`` are set and ``dtend`` is not strictly after
            ``dtstart``.
        """
        # Accepting a plain dict here means callers can write
        # EventData(x_properties={"X-FOO": "bar"}) without importing XProperties.
        if isinstance(self.x_properties, dict):
            self.x_properties = XProperties(self.x_properties)

        self._reconcile_all_day()

        if self.all_day:
            # DATE values carry no TZID by definition; keeping one would emit invalid ical.
            self.dtstart_tzid = ""
            self.dtend_tzid = ""
        else:
            self.dtstart, self.dtstart_tzid = _to_utc_aware(
                self.dtstart, self.dtstart_tzid, "dtstart"
            )
            self.dtend, self.dtend_tzid = _to_utc_aware(
                self.dtend, self.dtend_tzid, "dtend"
            )

        # DTSTAMP and LAST-MODIFIED are UTC by spec, so they need no companion TZID.
        self.created_at, _ = _to_utc_aware(self.created_at, "", "created_at")
        self.changed_at, _ = _to_utc_aware(self.changed_at, "", "changed_at")

        if self.dtstart is not None and self.dtend is not None:
            if self.dtend <= self.dtstart:
                raise ValueError(
                    f"dtend ({self.dtend}) must be strictly after dtstart ({self.dtstart}). "
                    "Remember that DTEND is exclusive: a single all-day event on "
                    "2026-03-04 has dtend=2026-03-05. Use the last_day property to read "
                    "the inclusive final day of an all-day event."
                )

        # A UID is minted only for events this process invented. Assigning one to a
        # component that came off a server would silently fork it into a second event.
        if not self.uid and self._raw_component is None:
            self.uid = str(uuid4())

    def _reconcile_all_day(self) -> None:
        """Make ``all_day`` agree with the actual types of ``dtstart`` / ``dtend``.

        Notes
        -----
        Coercive rather than strict, because a dataclass cannot distinguish "the caller
        explicitly passed ``all_day=False``" from "the caller left the default alone".
        Raising on a mismatch would therefore reject the perfectly reasonable
        ``EventData(dtstart=date(2026, 3, 4))``.

        Every type test uses ``isinstance(x, datetime.datetime)`` and never
        ``isinstance(x, datetime.date)``: a ``datetime`` is also a ``date``, so the
        latter matches both and is the classic way to get this wrong.
        """
        anchor = self.dtstart if self.dtstart is not None else self.dtend
        if anchor is None:
            return  # Nothing to reconcile against; keep the flag as given.

        anchor_is_date_only = not isinstance(anchor, datetime.datetime)

        if self.all_day and not anchor_is_date_only:
            logger.debug(
                "all_day=True with datetime values; truncating dtstart/dtend to dates."
            )
            if isinstance(self.dtstart, datetime.datetime):
                self.dtstart = self.dtstart.date()
            if isinstance(self.dtend, datetime.datetime):
                self.dtend = self.dtend.date()
        elif not self.all_day and anchor_is_date_only:
            logger.debug("Date-only dtstart given; setting all_day=True.")
            self.all_day = True
            # A mixed pair (date start, datetime end) is nonsense in iCal; align the end.
            if isinstance(self.dtend, datetime.datetime):
                self.dtend = self.dtend.date()
        elif self.all_day and anchor_is_date_only:
            # Already consistent, but the other end may still be a datetime.
            if isinstance(self.dtstart, datetime.datetime):
                self.dtstart = self.dtstart.date()
            if isinstance(self.dtend, datetime.datetime):
                self.dtend = self.dtend.date()

    # --- Convenience properties -------------------------------------------------

    @property
    def dtstart_local(self) -> Optional[datetime.datetime | datetime.date]:
        """``dtstart`` rendered back in its original timezone.

        Returns
        -------
        datetime.datetime or datetime.date or None
            An all-day event's ``date`` is returned unchanged, since it has no zone.
        """
        return self._to_local(self.dtstart, self.dtstart_tzid)

    @property
    def dtend_local(self) -> Optional[datetime.datetime | datetime.date]:
        """``dtend`` rendered back in its original timezone."""
        return self._to_local(self.dtend, self.dtend_tzid)

    @staticmethod
    def _to_local(
        value: Optional[datetime.datetime | datetime.date], tzid: str
    ) -> Optional[datetime.datetime | datetime.date]:
        """Convert a stored UTC value back into `tzid`, leaving dates untouched."""
        if value is None or not isinstance(value, datetime.datetime):
            return value
        zone = _resolve_zone(tzid)
        return value.astimezone(zone) if zone is not None else value

    @property
    def effective_dtend(self) -> Optional[datetime.datetime | datetime.date]:
        """``dtend``, or the RFC 5545 default when the event carries none.

        Returns
        -------
        datetime.datetime or datetime.date or None
            ``None`` only when ``dtstart`` is also unset.

        Notes
        -----
        RFC 5545 section 3.6.1: a VEVENT with a DATE ``dtstart`` and no DTEND lasts one
        day; one with a DATE-TIME ``dtstart`` and no DTEND has zero duration. Reading
        ``dtend`` directly would report ``None`` for both, which is faithful to the wire
        format but useless for laying out a calendar.
        """
        if self.dtend is not None:
            return self.dtend
        if self.dtstart is None:
            return None
        if self.all_day:
            return self.dtstart + datetime.timedelta(days=1)
        return self.dtstart

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        """Length of the event, using :attr:`effective_dtend`."""
        end = self.effective_dtend
        if self.dtstart is None or end is None:
            return None
        return end - self.dtstart

    @property
    def last_day(self) -> Optional[datetime.date]:
        """Inclusive final day of an all-day event.

        Returns
        -------
        datetime.date or None
            ``None`` for timed events.

        Notes
        -----
        Exists so that callers displaying "4 to 6 March" do not reach for ``dtend`` and
        render an extra day, and are not tempted to "fix" the exclusive DTEND in place.
        """
        if not self.all_day:
            return None
        end = self.effective_dtend
        if end is None:
            return None
        return end - datetime.timedelta(days=1)

    @property
    def is_recurring(self) -> bool:
        """Whether the event carries any recurrence rule or explicit recurrence date."""
        return bool(self.rrule or self.rdate)

    # --- Representations --------------------------------------------------------

    def __str__(self) -> str:
        """Return a compact multi-line summary, skipping empty fields."""
        lines = [f"<EventData UID: {self.uid}>"]
        for f in fields(self.__class__):
            if f.name == "uid" or f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, (list, XProperties, dict)) and not value:
                continue
            if isinstance(value, str) and not value:
                continue
            if value is None:
                continue
            if isinstance(value, str) and len(value) > 70:
                value = value[:67] + "..."
            lines.append(f"  {f.name}: {value}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        """Return an exhaustive representation, one field per line."""
        lines = [f"{self.__class__.__name__}("]
        for f in fields(self.__class__):
            if f.name.startswith("_"):
                continue
            lines.append(f"    {f.name}={getattr(self, f.name)!r},")
        lines.append(")")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a plain JSON-serializable dictionary.

        Returns
        -------
        dict
            Datetimes and dates become ISO 8601 strings. Private fields, including the
            retained raw component, are excluded: they are neither serializable nor
            meaningful outside this process.
        """

        def iso(value: Optional[datetime.datetime | datetime.date]) -> Optional[str]:
            return value.isoformat() if value is not None else None

        return {
            "uid": self.uid,
            "calendar_uid": self.calendar_uid,
            "summary": self.summary,
            "description": self.description,
            "location": self.location,
            "status": self.status,
            "all_day": self.all_day,
            "dtstart": iso(self.dtstart),
            "dtstart_tzid": self.dtstart_tzid,
            "dtend": iso(self.dtend),
            "dtend_tzid": self.dtend_tzid,
            "categories": list(self.categories),
            "rrule": self.rrule,
            "rdate": list(self.rdate),
            "exdate": list(self.exdate),
            "sequence": self.sequence,
            "created_at": iso(self.created_at),
            "changed_at": iso(self.changed_at),
            "synced": self.synced,
            "x_properties": self.x_properties.get_raw_properties(),
        }
