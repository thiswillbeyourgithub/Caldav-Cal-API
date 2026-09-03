"""
Shared fixtures for the server-backed tests.

These tests run against a real CalDAV server rather than mocks, mirroring the sibling
caldav_tasks_api project: the bugs worth catching in a CalDAV client are the ones that
only appear when a real server rewrites, reorders or normalizes what you sent it, and a
mock would happily reproduce your own assumptions instead.

They are skipped entirely when the CALDAV_CAL_API_TEST_* variables are not set, so a
bare checkout still runs the offline suite.
"""

import os

import pytest
from dotenv import load_dotenv

from caldav_cal_api import CalendarAPI

load_dotenv()


@pytest.fixture(scope="session")
def caldav_credentials():
    """Credentials for the test server, or a skip when they are not configured."""
    url = os.environ.get("CALDAV_CAL_API_TEST_URL")
    username = os.environ.get("CALDAV_CAL_API_TEST_USERNAME")
    password = os.environ.get("CALDAV_CAL_API_TEST_PASSWORD")

    if not all([url, username, password]):
        pytest.skip(
            "CALDAV_CAL_API_TEST_URL, CALDAV_CAL_API_TEST_USERNAME and "
            "CALDAV_CAL_API_TEST_PASSWORD must be set to run the server-backed tests."
        )
    return {"url": url, "username": username, "password": password}


@pytest.fixture(scope="session")
def test_calendar_name():
    """Name of the calendar the write tests are allowed to modify.

    Notes
    -----
    Required rather than defaulted, so that a misconfiguration can never cause the write
    tests to create and delete events in the user's real calendar.
    """
    name = os.environ.get("CALDAV_CAL_API_TEST_CALENDAR_NAME")
    if name is None:
        pytest.skip(
            "CALDAV_CAL_API_TEST_CALENDAR_NAME is not set. Point it at a scratch "
            "calendar; the write tests create and delete events in it."
        )
    name = name.strip()
    if not name:
        pytest.skip("CALDAV_CAL_API_TEST_CALENDAR_NAME is set but empty.")
    return name


@pytest.fixture(scope="session")
def api(caldav_credentials, test_calendar_name):
    """A connected, loaded API restricted to the scratch calendar."""
    try:
        instance = CalendarAPI(
            url=caldav_credentials["url"],
            username=caldav_credentials["username"],
            password=caldav_credentials["password"],
            nextcloud_mode=True,
            target_calendars=[test_calendar_name],
        )
    except ConnectionError as e:
        pytest.fail(f"Could not connect to the test CalDAV server: {e}")

    instance.load_remote_data()
    if not instance.calendars:
        pytest.skip(
            f"No calendar named '{test_calendar_name}' supporting VEVENT was found "
            "on the test server."
        )
    return instance


@pytest.fixture(scope="session")
def read_only_api(caldav_credentials, test_calendar_name):
    """The same connection, opened read-only, for the permission guard tests."""
    instance = CalendarAPI(
        url=caldav_credentials["url"],
        username=caldav_credentials["username"],
        password=caldav_credentials["password"],
        nextcloud_mode=True,
        target_calendars=[test_calendar_name],
        read_only=True,
    )
    instance.load_remote_data()
    return instance


@pytest.fixture
def scratch_calendar_uid(api, test_calendar_name):
    """UID of the calendar the write tests may modify."""
    for calendar in api.calendars:
        if calendar.name == test_calendar_name:
            return calendar.uid
    return api.calendars[0].uid
