# Final parser review fix

## Scope

- `scripts/rki_grabber/parser.py`
- `tests/test_rki_parser.py`

## RED

Added one parametrized real-parser regression for duplicate canonical PDF URLs:

- checksum-free anchor then MD5 anchor;
- MD5 anchor then checksum-free anchor.

Before production change: first order failed because retained candidate had
`expected_md5 is None`; reverse already passed. Existing conflicting-MD5 test
continues to require rejection.

Command:

```text
.venv/bin/pytest -q tests/test_rki_parser.py -k retains_md5
1 failed, 1 passed, 6 deselected
```

## GREEN

Replacement now occurs only for a new candidate or when retained candidate has
no MD5 and duplicate candidate supplies one. Existing two-non-null-different
MD5 conflict branch remains unchanged. Existing ordering and bitstream-id
deduplication remain unchanged.

Commands:

```text
.venv/bin/pytest -q tests/test_rki_parser.py -k retains_md5
2 passed, 6 deselected

.venv/bin/pytest -q tests/test_rki_grabber.py tests/test_rki_parser.py tests/test_rki_http.py tests/test_download_security.py tests/test_grabber_api.py
23 passed

.venv/bin/python scripts/validate_p03_grabber.py
P03 grabber: ok; modular API/CLI; same-origin HTTPS; robots fail-closed; bounded PDF downloads
```

## Concern

None. Unrelated in-progress files remained unstaged.
