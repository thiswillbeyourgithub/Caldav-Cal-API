"""
Offline tests for the ETag normalization applied before a write.

Apache appends a content-encoding suffix to the ETag of every response it compresses,
so the same event is advertised as '"abc"' when served plain and '"abc-gzip"' when
served compressed. The caldav library echoes whatever ETag it holds back as an
``If-Match`` header, and the server compares that against the stored, unsuffixed value:
sending the suffixed form fails the precondition and the write is rejected with 412.

This bit only in practice on events whose payload was large enough to be compressed. A
``DTSTART;TZID=`` drags a whole VTIMEZONE into the body and pushes it over the
threshold, so a recurring Europe/Paris event failed to update while the same event in
plain UTC succeeded.
"""

import pytest
from caldav.elements import dav

from caldav_cal_api.caldav_cal_api import _normalize_etag


class _FakeServerEvent:
    """Just enough of a caldav.Event for the helper: a props dict and an etag view."""

    def __init__(self, etag):
        self.props = {}
        if etag is not None:
            self.props[dav.GetEtag.tag] = etag

    @property
    def etag(self):
        return self.props.get(dav.GetEtag.tag)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The regression: Apache's mod_deflate suffix must go.
        (
            '"1ec7176d476a6363cf29f23fb8cbffa3-gzip"',
            '"1ec7176d476a6363cf29f23fb8cbffa3"',
        ),
        # Brotli and deflate use the same convention.
        ('"abc123-br"', '"abc123"'),
        ('"abc123-deflate"', '"abc123"'),
        # A weak validator keeps its W/ prefix.
        ('W/"abc123-gzip"', 'W/"abc123"'),
        # An untouched ETag must survive verbatim.
        ('"b94ebf7250433a8d0430f785ae109aaf"', '"b94ebf7250433a8d0430f785ae109aaf"'),
        # A hyphen inside the token is not a suffix and must not be trimmed.
        ('"abc-123"', '"abc-123"'),
        # Neither is a suffix-shaped tail that is not one of the known encodings.
        ('"abc-zip"', '"abc-zip"'),
        # Unquoted values are left alone rather than guessed at.
        ("abc123-gzip", "abc123-gzip"),
    ],
)
def test_encoding_suffixes_are_stripped(raw, expected):
    """Only a known content-encoding suffix is removed, and nothing else changes."""
    server_event = _FakeServerEvent(raw)
    _normalize_etag(server_event)
    assert server_event.etag == expected


def test_a_missing_etag_is_left_alone():
    """No ETag means no If-Match, which is a valid state and not an error."""
    server_event = _FakeServerEvent(None)
    _normalize_etag(server_event)
    assert server_event.etag is None
