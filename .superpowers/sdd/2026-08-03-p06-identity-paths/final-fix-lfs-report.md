# Final fix: LFS Markdown inventory

## RED

`test_lfs_reference_inventory_excludes_noncanonical_markdown` failed before
production change: `README.md` and `Jahre/1994/notes.md` appeared in
`list_references()`.

## GREEN

`.md` files now require a `Markdown` path segment. PDF and ZIP selection stays
unchanged.

## Verification

- `pytest -q tests/test_storage_lfs.py::test_lfs_reference_inventory_excludes_noncanonical_markdown` — 1 passed
- `pytest -q tests/test_storage_lfs.py tests/test_storage_cli.py tests/test_storage_migration.py tests/test_storage_remote.py tests/test_storage_contract.py tests/test_storage_config_boundaries.py tests/test_validate_p04_storage.py` — 59 passed
- `python scripts/validate_p04_storage.py` — passed

## Concern

No concern. Canonical document paths place Markdown output below a `Markdown`
segment; PDFs and ZIPs remain global below artifact root.
