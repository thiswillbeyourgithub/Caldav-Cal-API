"""
Offline tests for the ETag normalization applied before a write.

A compressing HTTP layer appends the content coding it applied to the ETag of the
response, so the same event is advertised as '"abc"' when served plain and '"abc-gzip"'
when served compressed. The caldav library echoes whatever ETag it holds back as an
``If-Match`` header, and the server compares that against the stored, unsuffixed value:
sending the suffixed form fails the precondition and the write is rejected with 412.

This bit only on events whose payload was large enough to be compressed. A
``DTSTART;TZID=`` drags a whole VTIMEZONE into the body and pushes it over the threshold,
so a recurring Europe/Paris event failed to update while the same event in plain UTC
succeeded.

Which coding appears depends on what the client negotiated, not on the server. The same
Nextcloud handed back ``-gzip`` to a plain urllib3 and ``-zstd`` to an environment with
urllib3-future and brotli installed, which is why the fix cannot special-case gzip and
why it prefers asking the server over trimming the string.
"""

import pytest
from caldav.elements import dav

from caldav_cal_api.caldav_cal_api import (
    _normalize_etag,
    _strip_etag_encoding_suffix,
)


class _FakeServerEvent:
    """Enough of a caldav.Event for the helper: props, an etag view, and a PROPFIND."""

    def __init__(self, etag, propfind_result=None, propfind_raises=False):
        self.props = {}
        if etag is not None:
            self.props[dav.GetEtag.tag] = etag
        self._propfind_result = propfind_result
        self._propfind_raises = propfind_raises
        self.propfind_calls = 0

    @property
    def etag(self):
        return self.props.get(dav.GetEtag.tag)

    def get_property(self, prop, use_cached=False):
        self.propfind_calls += 1
        if self._propfind_raises:
            raise RuntimeError("server does not answer that PROPFIND")
        return self._propfind_result


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The codings actually observed against Nextcloud, from two different clients.
        (
            '"1ec7176d476a6363cf29f23fb8cbffa3-gzip"',
            '"1ec7176d476a6363cf29f23fb8cbffa3"',
        ),
        (
            '"cc12c2aee263e05e533458092636bdb6-zstd"',
            '"cc12c2aee263e05e533458092636bdb6"',
        ),
        ('"abc123-br"', '"abc123"'),
        ('"abc123-deflate"', '"abc123"'),
        # A chain of filters can append more than one.
        ('"abc123-gzip-br"', '"abc123"'),
        # A weak validator keeps its W/ prefix.
        ('W/"abc123-gzip"', 'W/"abc123"'),
        # An untouched ETag must survive verbatim.
        ('"b94ebf7250433a8d0430f785ae109aaf"', '"b94ebf7250433a8d0430f785ae109aaf"'),
        # A hyphen inside the token is not a suffix and must not be trimmed.
        ('"abc-123"', '"abc-123"'),
        # Neither is a suffix-shaped tail that is not a known content coding.
        ('"abc-zip"', '"abc-zip"'),
        # Unquoted values are left alone rather than guessed at.
        ("abc123-gzip", "abc123-gzip"),
    ],
)
def test_encoding_suffixes_are_stripped(raw, expected):
    """Only a known content-coding suffix is removed, and nothing else changes."""
    assert _strip_etag_encoding_suffix(raw) == expected


def test_a_mangled_etag_is_replaced_by_the_value_the_server_reports():
    """The WebDAV property is authoritative, so it wins over trimming the string.

    Only the HTTP header is rewritten by the compressing layer. The getetag property
    travels inside the XML body of a PROPFIND and arrives intact.
    """
    server_event = _FakeServerEvent(
        '"81078e284349937473ade4631d1d3fd3-zstd"',
        propfind_result='"81078e284349937473ade4631d1d3fd3"',
    )
    _normalize_etag(server_event)
    assert server_event.etag == '"81078e284349937473ade4631d1d3fd3"'
    assert server_event.propfind_calls == 1


def test_a_clean_etag_costs_no_extra_round_trip():
    """No suffix means the header is already the stored value, so do not ask again."""
    server_event = _FakeServerEvent('"b94ebf7250433a8d0430f785ae109aaf"')
    _normalize_etag(server_event)
    assert server_event.etag == '"b94ebf7250433a8d0430f785ae109aaf"'
    assert server_event.propfind_calls == 0


def test_trimming_is_the_fallback_when_the_propfind_fails():
    """A guess still beats sending a token that is certain to be rejected."""
    server_event = _FakeServerEvent('"abc123-gzip"', propfind_raises=True)
    _normalize_etag(server_event)
    assert server_event.etag == '"abc123"'
    assert server_event.propfind_calls == 1


def test_trimming_is_the_fallback_when_the_propfind_is_empty():
    """An empty answer is no answer, so fall back rather than clearing the ETag."""
    server_event = _FakeServerEvent('"abc123-br"', propfind_result=None)
    _normalize_etag(server_event)
    assert server_event.etag == '"abc123"'


def test_a_missing_etag_is_left_alone():
    """No ETag means no If-Match, which is a valid state and not an error."""
    server_event = _FakeServerEvent(None)
    _normalize_etag(server_event)
    assert server_event.etag is None
    assert server_event.propfind_calls == 0
