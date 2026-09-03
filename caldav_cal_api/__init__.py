"""
Caldav Cal API package.

Provides the :class:`~caldav_cal_api.caldav_cal_api.CalendarAPI` client for reading and
writing CalDAV calendar events (VEVENT), plus the data structures used to represent
them: :class:`CalendarData`, :class:`EventData` and :class:`Occurrence`.

This is the calendar counterpart of the sibling ``caldav_tasks_api`` project, which
covers tasks (VTODO).
"""

# Imported first so that logging is configured before any other module emits a record.
from caldav_cal_api.utils import logging_config  # noqa: F401  (import for side effect)

from caldav_cal_api.caldav_cal_api import CalendarAPI
from caldav_cal_api.utils.data import CalendarData, EventData, Occurrence, XProperties

VERSION = CalendarAPI.VERSION

__all__ = [
    "CalendarAPI",
    "CalendarData",
    "EventData",
    "Occurrence",
    # Unlike the sibling, XProperties is exported at top level: users routinely need to
    # build one when creating an event carrying custom X- properties.
    "XProperties",
    "logging_config",
    "VERSION",
]
