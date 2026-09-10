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

import copy
import datetime
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional, TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import icalendar
from icalendar.parser import Contentline
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

        # DTSTAMP is mandatory in a VEVENT, so it is settled here rather than at write
        # time: to_ical() must be a pure function of the object for the round-trip
        # guarantee (to_ical -> from_ical -> to_ical is byte-identical) to hold.
        # Microseconds are dropped because iCal has no sub-second precision, and keeping
        # them would make an object differ from its own parsed copy.
        now_utc = datetime.datetime.now(UTC).replace(microsecond=0)
        if self.created_at is None:
            self.created_at = now_utc
        self.created_at = self.created_at.replace(microsecond=0)

        # LAST-MODIFIED, unlike DTSTAMP, is optional, so an absent one is only invented
        # for events this process built. Minting one for a parsed component would put a
        # value in the object that the server never sent, and since it is "now" it would
        # differ on every parse: a dump of an unchanged calendar would show every event
        # as modified on every run.
        if self.changed_at is None and self._raw_component is None:
            self.changed_at = now_utc
        if self.changed_at is not None:
            self.changed_at = self.changed_at.replace(microsecond=0)

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

    # --- Serialization ----------------------------------------------------------

    # Properties this class owns. On write they are deleted from the retained source
    # component and rewritten from the dataclass fields; everything else the server sent
    # (ATTENDEE, ORGANIZER, VALARM, TRANSP, CLASS, GEO, URL, ATTACH, RELATED-TO, ...) is
    # left exactly as it was. Without this, updating a meeting's summary would strip its
    # participants and everyone's reminders, because this class does not model them.
    _MANAGED_PROPERTIES = frozenset(
        {
            "UID",
            "SUMMARY",
            "DESCRIPTION",
            "LOCATION",
            "STATUS",
            "CATEGORIES",
            "DTSTART",
            "DTEND",
            "DURATION",
            "RRULE",
            "RDATE",
            "EXDATE",
            "DTSTAMP",
            "LAST-MODIFIED",
            "SEQUENCE",
        }
    )

    def _build_component(self) -> icalendar.Event:
        """Produce the icalendar component representing this event.

        Returns
        -------
        icalendar.Event
            A copy of the retained source component (or a fresh one) with every managed
            property rewritten from the dataclass fields.

        Notes
        -----
        Both serialization entry points funnel through here, and the managed properties
        are always deleted then re-added in the same fixed order. That is what makes
        ``to_ical() -> from_ical() -> to_ical()`` byte-identical: unmanaged properties
        keep their relative position, managed ones are always appended in one canonical
        sequence, so a second pass reproduces the first.
        """
        component = (
            copy.deepcopy(self._raw_component)
            if self._raw_component is not None
            else icalendar.Event()
        )

        for key in list(component.keys()):
            upper = str(key).upper()
            if upper in self._MANAGED_PROPERTIES or upper.startswith("X-"):
                del component[key]

        component.add("UID", self.uid)
        if self.created_at is not None:
            component.add("DTSTAMP", self.created_at)

        if self.dtstart is not None:
            # The localized value is written, not the UTC one, so the emitted line
            # carries DTSTART;TZID=... and the event keeps the zone it was authored in.
            component.add("DTSTART", self.dtstart_local)

        if self._should_write_duration():
            component.add("DURATION", self._source_duration)
        elif self.dtend is not None:
            component.add("DTEND", self.dtend_local)

        if self.summary:
            component.add("SUMMARY", self.summary)
        if self.description:
            component.add("DESCRIPTION", self.description)
        if self.location:
            component.add("LOCATION", self.location)
        if self.status:
            component.add("STATUS", self.status)
        if self.categories:
            component.add("CATEGORIES", self.categories)

        if self.rrule:
            component.add("RRULE", icalendar.prop.vRecur.from_ical(self.rrule))
        self._add_raw_date_lines(component=component, name="RDATE", lines=self.rdate)
        self._add_raw_date_lines(component=component, name="EXDATE", lines=self.exdate)

        if self.changed_at is not None:
            component.add("LAST-MODIFIED", self.changed_at)
        if self.sequence:
            component.add("SEQUENCE", self.sequence)

        for raw_key, raw_value in self.x_properties.items():
            # The stored key keeps any parameters the server sent, e.g.
            # "X-APPLE-SORT-ORDER;VALUE=TEXT", so it is split back apart here.
            prop_name, _, param_str = raw_key.partition(";")
            prop = icalendar.prop.vText(raw_value)
            if param_str:
                for chunk in param_str.split(";"):
                    param_key, _, param_value = chunk.partition("=")
                    prop.params[param_key] = param_value
            component.add(prop_name, prop, encode=0)

        return component

    def _should_write_duration(self) -> bool:
        """Whether to emit DURATION instead of DTEND.

        Returns
        -------
        bool
            True only when the source used DURATION *and* the event has not been retimed
            since.

        Notes
        -----
        DTEND and DURATION are mutually exclusive in a VEVENT. Writing DURATION back
        when it is still accurate preserves byte-level fidelity with the server copy and
        keeps the nominal-duration semantics that matter for a recurring event crossing
        a DST boundary. Once the caller changes dtstart or dtend the stored duration no
        longer describes the event, so DTEND wins.
        """
        if self._source_duration is None:
            return False
        if self.dtstart is None or self.dtend is None:
            return False
        return (self.dtend - self.dtstart) == self._source_duration

    @staticmethod
    def _add_raw_date_lines(
        *, component: icalendar.Event, name: str, lines: list[str]
    ) -> None:
        """Re-add stored RDATE/EXDATE content lines to `component`.

        Parameters
        ----------
        component : icalendar.Event
            Component to mutate.
        name : str
            Either ``"RDATE"`` or ``"EXDATE"``.
        lines : list of str
            Full content lines as stored, e.g.
            ``"EXDATE;TZID=Europe/Paris:20260121T090000"``.

        Notes
        -----
        These are kept as whole content lines rather than parsed values because that is
        precisely the form ``dateutil.rrule.rrulestr`` consumes, which is what makes the
        "store recurrence raw, expand on demand" design cheap. Reconstructing the
        property object here costs one parse and keeps parameters (TZID, VALUE=DATE)
        intact.
        """
        for line in lines:
            try:
                _, params, value = Contentline(line).parts()
                values = icalendar.prop.vDDDLists.from_ical(
                    value, timezone=params.get("TZID")
                )
                prop = icalendar.prop.vDDDLists(values)
                prop.params = params
                component.add(name, prop, encode=0)
            except Exception as e:
                logger.warning(f"Dropping unparseable {name} line '{line}': {e}")

    def to_ical(self) -> str:
        """Serialize to a bare VEVENT block.

        Returns
        -------
        str
            The component's iCalendar text, CRLF-terminated as RFC 5545 requires.

        Notes
        -----
        Unlike the sibling caldav_tasks_api, which hand-writes LF-separated lines, this
        goes through the icalendar library and therefore emits CRLF and folds long
        lines. Use :meth:`to_vcalendar` for anything actually sent to a server.
        """
        return self._build_component().to_ical().decode("utf-8")

    def to_vcalendar(self, *, prodid: str = "-//caldav_cal_api//EN") -> str:
        """Serialize to a complete VCALENDAR, ready to PUT to a server.

        Parameters
        ----------
        prodid : str, optional
            PRODID to advertise.

        Returns
        -------
        str
            A VCALENDAR containing this event plus any VTIMEZONE its TZIDs require.

        Notes
        -----
        A ``DTSTART;TZID=Europe/Paris`` with no matching VTIMEZONE component is invalid
        per RFC 5545 and some servers reject it outright, so the timezones are
        synthesized here rather than left to the caller.
        """
        calendar = icalendar.Calendar()
        calendar.add("PRODID", prodid)
        calendar.add("VERSION", "2.0")
        calendar.add_component(self._build_component())
        calendar.add_missing_timezones()
        return calendar.to_ical().decode("utf-8")

    @staticmethod
    def from_ical(
        ical: str | bytes | icalendar.Event, calendar_uid: str = ""
    ) -> "EventData":
        """Build an :class:`EventData` from iCalendar data.

        Parameters
        ----------
        ical : str or bytes or icalendar.Event
            A VEVENT block, a VCALENDAR wrapping one, or an already-parsed component.
        calendar_uid : str, optional
            UID of the owning calendar, recorded on the result.

        Returns
        -------
        EventData
            The master component. ``synced`` is left False: it is a statement about the
            server that only :class:`~caldav_cal_api.caldav_cal_api.CalendarAPI` may make.

        Raises
        ------
        ValueError
            If no VEVENT can be found in the input.

        Notes
        -----
        When several VEVENTs share a UID, the one *without* a RECURRENCE-ID is the
        master and the others are per-occurrence overrides. This library deliberately
        does not model overrides, so they are dropped with a warning rather than
        silently: losing an override changes what the user sees, and they deserve to
        know it happened.
        """
        component = EventData._select_master_component(ical)

        def text(name: str) -> str:
            value = component.get(name)
            if value is None:
                return ""
            if isinstance(value, list):  # Repeated property: keep the first occurrence.
                value = value[0]
            return str(value)

        dtstart, dtstart_tzid = EventData._read_datetime_property(component, "DTSTART")
        dtend, dtend_tzid = EventData._read_datetime_property(component, "DTEND")

        source_duration: Optional[datetime.timedelta] = None
        if (
            dtend is None
            and component.get("DURATION") is not None
            and dtstart is not None
        ):
            source_duration = component["DURATION"].dt
            dtend = dtstart + source_duration
            dtend_tzid = dtstart_tzid

        created_at, _ = EventData._read_datetime_property(component, "DTSTAMP")
        changed_at, _ = EventData._read_datetime_property(component, "LAST-MODIFIED")

        try:
            sequence = int(component.get("SEQUENCE", 0))
        except (TypeError, ValueError):
            logger.warning(
                f"Unparseable SEQUENCE '{component.get('SEQUENCE')}'; using 0."
            )
            sequence = 0

        event = EventData(
            calendar_uid=calendar_uid,
            categories=EventData._read_categories(component),
            changed_at=changed_at,
            created_at=created_at,
            description=text("DESCRIPTION"),
            dtend=dtend,
            dtend_tzid=dtend_tzid,
            dtstart=dtstart,
            dtstart_tzid=dtstart_tzid,
            exdate=EventData._read_raw_date_lines(component, "EXDATE"),
            location=text("LOCATION"),
            rdate=EventData._read_raw_date_lines(component, "RDATE"),
            rrule=EventData._read_rrule(component),
            sequence=sequence,
            status=text("STATUS"),
            summary=text("SUMMARY"),
            uid=text("UID"),
            x_properties=EventData._read_x_properties(component),
            _raw_component=component,
            _source_duration=source_duration,
        )
        return event

    @staticmethod
    def _select_master_component(
        ical: str | bytes | icalendar.Event,
    ) -> icalendar.Event:
        """Find the master VEVENT in `ical`, warning about any dropped overrides."""
        if isinstance(ical, icalendar.Event):
            return ical

        text = (
            ical.decode("utf-8", errors="replace") if isinstance(ical, bytes) else ical
        )

        components: list[icalendar.Event] = []
        try:
            parsed = icalendar.Calendar.from_ical(text)
            components = list(parsed.walk("VEVENT"))
        except Exception:
            components = []

        if not components:
            # A bare "BEGIN:VEVENT ... END:VEVENT" block has no enclosing VCALENDAR.
            try:
                components = [icalendar.Event.from_ical(text)]
            except Exception as e:
                raise ValueError(f"Could not parse any VEVENT from the input: {e}")

        masters = [c for c in components if "RECURRENCE-ID" not in c]
        if not masters:
            # Degenerate but real: a server may hand back only an override.
            logger.warning(
                "No master VEVENT found (every component has a RECURRENCE-ID). "
                "Using the first override as if it were the master."
            )
            return components[0]

        dropped = len(components) - 1
        if dropped > 0:
            logger.warning(
                f"Dropping {dropped} RECURRENCE-ID override(s) for UID "
                f"'{masters[0].get('UID')}': this library models the master event only, "
                "so per-occurrence modifications are not represented."
            )
        return masters[0]

    @staticmethod
    def _read_datetime_property(
        component: icalendar.Event, name: str
    ) -> tuple[Optional[datetime.datetime | datetime.date], str]:
        """Read a date/date-time property and its TZID parameter.

        Returns
        -------
        tuple of (datetime.datetime or datetime.date or None, str)

        Notes
        -----
        The DATE vs DATE-TIME distinction comes from the parsed type, never from
        inspecting the string for a ``"T"``: icalendar reports ``VALUE=DATE``
        structurally and getting this from the text is how off-by-one-day bugs start.
        """
        prop = component.get(name)
        if prop is None:
            return None, ""
        if isinstance(prop, list):
            prop = prop[0]
        return prop.dt, str(prop.params.get("TZID", ""))

    @staticmethod
    def _read_categories(component: icalendar.Event) -> list[str]:
        """Flatten CATEGORIES, which may appear as one property or several."""
        prop = component.get("CATEGORIES")
        if prop is None:
            return []
        props = prop if isinstance(prop, list) else [prop]
        categories: list[str] = []
        for item in props:
            cats = getattr(item, "cats", None)
            if cats is None:
                categories.append(str(item))
            else:
                categories.extend(str(cat) for cat in cats)
        return categories

    @staticmethod
    def _read_rrule(component: icalendar.Event) -> str:
        """Read RRULE back as its raw value string."""
        prop = component.get("RRULE")
        if prop is None:
            return ""
        if isinstance(prop, list):
            logger.warning(
                "Multiple RRULE properties found; RFC 5545 allows only one. "
                "Keeping the first and dropping the rest."
            )
            prop = prop[0]
        return prop.to_ical().decode("utf-8")

    @staticmethod
    def _read_raw_date_lines(component: icalendar.Event, name: str) -> list[str]:
        """Read RDATE/EXDATE back as full content lines.

        Notes
        -----
        Content lines rather than parsed values, because that is the form
        ``dateutil.rrule.rrulestr`` accepts directly. See :meth:`_add_raw_date_lines`.
        """
        prop = component.get(name)
        if prop is None:
            return []
        props = prop if isinstance(prop, list) else [prop]
        return [str(Contentline.from_parts(name, item.params, item)) for item in props]

    @staticmethod
    def _read_x_properties(component: icalendar.Event) -> XProperties:
        """Collect every ``X-`` property, preserving raw keys and their parameters."""
        collected: Dict[str, str] = {}
        for key in component.keys():
            if not str(key).upper().startswith("X-"):
                continue
            prop = component[key]
            if isinstance(prop, list):
                prop = prop[0]
            raw_key = str(key)
            params = getattr(prop, "params", {})
            if params:
                raw_key += ";" + ";".join(f"{k}={v}" for k, v in params.items())
            collected[raw_key] = str(prop)
        return XProperties(collected)

    # --- Recurrence expansion ---------------------------------------------------

    def get_occurrences(
        self,
        start: datetime.datetime | datetime.date,
        end: datetime.datetime | datetime.date,
        *,
        limit: int = 1000,
    ) -> list["Occurrence"]:
        """Expand this event's recurrence into concrete instances overlapping a window.

        Parameters
        ----------
        start, end : datetime.datetime or datetime.date
            Half-open window ``[start, end)``. Dates are read as midnight UTC; naive
            datetimes are assumed to be UTC.
        limit : int, optional
            Maximum number of occurrences to return.

        Returns
        -------
        list of Occurrence
            Sorted by start. Empty when the event never falls inside the window.

        Raises
        ------
        ValueError
            If `end` is before `start`.

        Notes
        -----
        An instance is included when it *overlaps* the window, not merely when it starts
        inside it, which is what a calendar view needs to draw an event already in
        progress at the left edge.

        Expansion runs in the event's original timezone and only then converts back to
        UTC. Expanding "every Monday 09:00 Europe/Paris" over UTC instants would drift by
        an hour at each DST transition; this is the reason ``dtstart_tzid`` is kept at
        all.

        A malformed rule is logged and degrades to "the master event only" rather than
        raising: one bad RRULE somewhere in a calendar must not break the whole view.
        """
        window_start = _as_utc_datetime(start)
        window_end = _as_utc_datetime(end)
        if window_end < window_start:
            raise ValueError(f"end ({end}) must not be before start ({start}).")

        if self.dtstart is None:
            return []

        duration = self.duration or datetime.timedelta(0)

        if not self.is_recurring:
            occurrence = Occurrence(
                all_day=self.all_day,
                dtend=self.effective_dtend,
                dtstart=self.dtstart,
                event=self,
            )
            return [occurrence] if occurrence.overlaps(window_start, window_end) else []

        # All-day values are DST-immune, so they expand safely anchored at midnight UTC.
        # Timed values must expand in their own zone, see the note above.
        zone = UTC if self.all_day else (_resolve_zone(self.dtstart_tzid) or UTC)
        anchor = self._expansion_anchor(zone)

        try:
            instances = self._expand_rule_set(
                anchor=anchor,
                zone=zone,
                window_start=window_start,
                window_end=window_end,
                duration=duration,
            )
        except Exception as e:
            logger.error(
                f"Could not expand recurrence for event '{self.uid}' "
                f"(rrule={self.rrule!r}): {e}. Falling back to the master event only."
            )
            instances = [anchor]

        occurrences: list[Occurrence] = []
        for instance in instances:
            occurrence_start = self._instance_to_stored_type(instance)
            occurrence_end = occurrence_start + duration
            occurrence = Occurrence(
                all_day=self.all_day,
                dtend=occurrence_end,
                dtstart=occurrence_start,
                event=self,
            )
            if not occurrence.overlaps(window_start, window_end):
                continue
            occurrences.append(occurrence)
            if len(occurrences) >= limit:
                logger.warning(
                    f"Occurrence limit of {limit} reached while expanding event "
                    f"'{self.uid}'. Narrow the window or raise the limit to see more."
                )
                break

        occurrences.sort(key=lambda occ: _as_utc_datetime(occ.dtstart))
        return occurrences

    def _expansion_anchor(self, zone: datetime.tzinfo) -> datetime.datetime:
        """Return the DTSTART to expand from, as an aware datetime in `zone`."""
        if self.all_day:
            return datetime.datetime.combine(
                self.dtstart, datetime.time.min, tzinfo=zone
            )
        return self.dtstart.astimezone(zone)

    def _instance_to_stored_type(
        self, instance: datetime.datetime
    ) -> datetime.datetime | datetime.date:
        """Convert one expanded instance back to the type this event stores."""
        return instance.date() if self.all_day else instance.astimezone(UTC)

    def _expand_rule_set(
        self,
        *,
        anchor: datetime.datetime,
        zone: datetime.tzinfo,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        duration: datetime.timedelta,
    ) -> list[datetime.datetime]:
        """Build and evaluate the dateutil rule set for this event.

        Returns
        -------
        list of datetime.datetime
            Instance start times, in `zone`, that could possibly overlap the window.

        Notes
        -----
        The set is queried with ``rruleset.between()`` and never materialized whole. An
        RRULE with no COUNT or UNTIL is infinite ("every Monday, forever"), which is both
        legal and common, so ``list(rule_set)`` would simply never return.

        The lower bound is pulled back by the event's duration so that an instance which
        started before the window but is still running inside it is not missed; the
        precise overlap test is applied by the caller.

        RDATE and EXDATE are parsed with icalendar and converted into `zone` here rather
        than handed to ``dateutil.rrule.rrulestr`` as text. dateutil's text parser has
        only partial support for the ``TZID`` parameter, and feeding it a zone it cannot
        resolve would fail the whole expansion over what is really a formatting detail.
        """
        from dateutil.rrule import rrulestr, rruleset

        rule_set = rruleset()
        if self.rrule:
            rule_set.rrule(rrulestr(self.rrule, dtstart=anchor))
        else:
            # RDATE-only events still have the master itself as an occurrence.
            rule_set.rdate(anchor)

        for value in self._parse_date_lines(self.rdate, zone=zone):
            rule_set.rdate(value)
        for value in self._parse_date_lines(self.exdate, zone=zone):
            rule_set.exdate(value)

        return rule_set.between(
            after=window_start.astimezone(zone) - duration,
            before=window_end.astimezone(zone),
            inc=True,
        )

    @staticmethod
    def _parse_date_lines(
        lines: list[str], *, zone: datetime.tzinfo
    ) -> list[datetime.datetime]:
        """Turn stored RDATE/EXDATE content lines into aware datetimes in `zone`."""
        results: list[datetime.datetime] = []
        for line in lines:
            try:
                _, params, value = Contentline(line).parts()
                for parsed in icalendar.prop.vDDDLists.from_ical(
                    value, timezone=params.get("TZID")
                ):
                    if isinstance(parsed, datetime.datetime):
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=zone)
                        results.append(parsed.astimezone(zone))
                    elif isinstance(parsed, datetime.date):
                        results.append(
                            datetime.datetime.combine(
                                parsed, datetime.time.min, tzinfo=zone
                            )
                        )
            except Exception as e:
                logger.warning(f"Ignoring unparseable recurrence line '{line}': {e}")
        return results

    def delete(self) -> bool:
        """Delete this event from the server.

        Returns
        -------
        bool
            True when the server accepted the deletion.

        Raises
        ------
        RuntimeError
            If the event was not obtained from a :class:`CalendarAPI`.
        ValueError
            If the event has no UID or no owning calendar.
        """
        if self._api_reference is None:
            raise RuntimeError(
                "This EventData has no API reference. Only events obtained from a "
                "CalendarAPI (via load_remote_data or add_event) can delete themselves."
            )
        if not self.uid or not self.calendar_uid:
            raise ValueError(
                f"Cannot delete an event without both a uid ('{self.uid}') and a "
                f"calendar_uid ('{self.calendar_uid}')."
            )
        # Logged before the call so the content is recoverable from the log if the
        # deletion turns out to have been a mistake.
        logger.info(
            f"Deleting event uid='{self.uid}' summary='{self.summary}' "
            f"dtstart='{self.dtstart}' from calendar '{self.calendar_uid}'."
        )
        return self._api_reference.delete_event_by_id(
            uid=self.uid, calendar_uid=self.calendar_uid
        )


def _as_utc_datetime(value: datetime.datetime | datetime.date) -> datetime.datetime:
    """Coerce a date or datetime into an aware UTC datetime for window comparisons.

    Notes
    -----
    Dates become midnight UTC and naive datetimes are assumed to be UTC, so that
    all-day and timed events can be compared against one window without the caller
    having to normalize anything first.
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime.datetime.combine(value, datetime.time.min, tzinfo=UTC)


@dataclass(frozen=True)
class Occurrence:
    """One expanded instance of a (possibly recurring) event.

    Notes
    -----
    Deliberately not an :class:`EventData`. A clone would carry the master's UID, which
    corrupts any UID-keyed cache and invites ``update_event()`` on a phantom that has no
    server representation. This library models the master event only; an occurrence is a
    read-only view for display and availability checks.
    """

    all_day: bool  # Mirrors the parent event
    dtend: datetime.datetime | datetime.date  # Exclusive, UTC-normalized when timed
    dtstart: datetime.datetime | datetime.date  # UTC-normalized when timed
    event: EventData = field(repr=False, compare=False)  # The master this came from

    @property
    def summary(self) -> str:
        """Summary of the parent event, for convenience when rendering an agenda."""
        return self.event.summary

    def overlaps(
        self, window_start: datetime.datetime, window_end: datetime.datetime
    ) -> bool:
        """Whether this instance intersects the half-open window ``[start, end)``."""
        occurrence_start = _as_utc_datetime(self.dtstart)
        occurrence_end = _as_utc_datetime(self.dtend)
        if occurrence_end == occurrence_start:
            # A zero-length event would never "overlap" anything, yet it is still on the
            # calendar at that instant, so treat its start as inclusive.
            return window_start <= occurrence_start < window_end
        return occurrence_start < window_end and occurrence_end > window_start

    def __str__(self) -> str:
        return f"<Occurrence {self.dtstart} -> {self.dtend}: '{self.event.summary}'>"
