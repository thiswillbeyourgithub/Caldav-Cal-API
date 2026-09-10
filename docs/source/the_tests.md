# Tests

Two suites live in `tests/`. The offline one runs anywhere; the server-backed one is skipped unless credentials are configured.

## Offline

No server, no credentials. Covers the data model, iCalendar serialization and recurrence expansion.

| Area | Feature tested | Test function |
| --- | --- | --- |
| Parsing | Core fields off a realistic VEVENT | `test_parses_core_fields` |
| Timezones | UTC normalization with the TZID preserved | `test_timezone_is_normalized_to_utc_but_tzid_is_kept` |
| Timezones | An unknown TZID degrades instead of raising | `test_unknown_tzid_falls_back_to_utc_without_raising` |
| Serialization | Round-trip stability for a parsed event | `test_roundtrip_is_stable_for_a_parsed_event` |
| Serialization | Round-trip stability for a hand-built event | `test_roundtrip_is_stable_for_a_handbuilt_event` |
| Serialization | Attendees, organizer and alarms survive an edit | `test_unmodeled_properties_survive_an_update` |
| Serialization | DURATION kept until the event is retimed | `test_duration_is_preserved_when_the_event_is_not_retimed` |
| Serialization | VTIMEZONE emitted alongside a TZID reference | `test_to_vcalendar_emits_the_required_vtimezone` |
| Serialization | `to_dict()` is JSON-serializable | `test_to_dict_is_json_serializable` |
| All-day | Dates in, exclusive DTEND out | `test_all_day_event_uses_dates_and_exclusive_dtend` |
| All-day | The `all_day` flag is reconciled with the value types | `test_all_day_flag_is_reconciled_with_value_types` |
| All-day | A zero-length all-day event is rejected | `test_zero_length_all_day_event_is_rejected` |
| Defaults | RFC 5545 `effective_dtend` fallbacks | `test_effective_dtend_applies_rfc_defaults` |
| Recurrence | Overrides dropped, master kept | `test_recurrence_id_overrides_are_dropped_with_the_master_kept` |
| Identity | UID minted for new events only | `test_uid_is_minted_for_new_events_but_never_for_parsed_ones` |
| X-props | Normalized attribute access | `test_xproperties_lookup_is_forgiving` |
| Expansion | 09:00 local held across a DST transition | `test_weekly_expansion_keeps_local_wall_clock_across_dst` |
| Expansion | EXDATE removes, RDATE adds | `test_exdate_removes_an_instance`, `test_rdate_adds_an_instance` |
| Expansion | Instances inherit the master's duration | `test_occurrences_carry_the_parent_duration` |
| Expansion | All-day recurrence yields dates | `test_all_day_recurrence_yields_dates` |
| Expansion | Non-recurring events, and window overlap | `test_non_recurring_event_yields_at_most_itself`, `test_window_matches_on_overlap_not_only_on_start` |
| Expansion | An unbounded rule is capped, not run forever | `test_limit_caps_the_result` |
| Expansion | An inverted window is rejected | `test_reversed_window_is_rejected` |
| Expansion | A malformed rule degrades to the master | `test_malformed_rrule_degrades_to_the_master_event` |
| Expansion | Same answer after a serialization round-trip | `test_recurrence_survives_a_serialization_roundtrip` |

## Server-backed

Real server, no mocks: the bugs worth catching in a CalDAV client are the ones that appear when a real server rewrites, reorders or normalizes what you sent it, and a mock would only reproduce your own assumptions.

| Area | Feature tested | Test function |
| --- | --- | --- |
| Connection | Calendars are discovered | `test_calendars_are_fetched` |
| CRUD | Create, update, delete with a reload between steps | `test_create_update_and_delete_an_event` |
| Recurrence | RRULE survives create, reload and update | `test_recurring_event_keeps_its_rrule_through_a_round_trip` |
| All-day | Dates and exclusive DTEND round-trip | `test_all_day_event_round_trips_as_dates` |
| Objects | `EventData.delete()` | `test_event_can_delete_itself` |
| Window | Out-of-window events are absent until `fetch_all` | `test_narrow_window_excludes_far_future_events` |
| Fidelity | A VALARM survives an update through us | `test_unmodeled_properties_survive_a_server_round_trip` |
| Guards | Read-only mode blocks all three writes | `test_read_only_mode_blocks_every_write` |
| CLI | Each command exits cleanly | `test_cli_commands_run_successfully` |

### Fixtures

Defined in `tests/conftest.py`, all session-scoped except the last:

- `caldav_credentials`: the test server's URL, username and password, or a skip.
- `test_calendar_name`: the scratch calendar's name. Required rather than defaulted, so a misconfiguration cannot make the write tests touch a real calendar.
- `api`: a connected, loaded `CalendarAPI` restricted to that calendar.
- `read_only_api`: the same connection with `read_only=True`.
- `scratch_calendar_uid`: the UID of the scratch calendar.

### Running

```bash
uv pip install -e ".[dev]"
pytest                       # Offline only, unless .env is configured
pytest tests/test_event_data.py tests/test_occurrences.py   # Offline explicitly
```

The server-backed tests need all four variables set, most easily in a `.env` file, which `python-dotenv` loads automatically:

```
CALDAV_CAL_API_TEST_URL=...
CALDAV_CAL_API_TEST_USERNAME=...
CALDAV_CAL_API_TEST_PASSWORD=...
CALDAV_CAL_API_TEST_CALENDAR_NAME=Scratch
```

They create and delete events in the named calendar, so point it at a scratch one.
