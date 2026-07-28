# Beitragen und Entwicklungsumgebung

## Voraussetzungen

- Python 3.12 oder neuer
- Node.js 24 und npm 11

## Reproduzierbare Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade "pip==26.1.2"
python3 -m pip install -r requirements-test.txt -r requirements-docs.txt
python3 -m pip check
npm ci --ignore-scripts
```

## Verbindliche Prüfungen für P01

```bash
python3 scripts/validate_dependency_locks.py
python3 scripts/validate_fixture_manifest.py
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
pytest -q
npm test
```

Tests sind standardmäßig netzwerkfrei. Ein späterer expliziter Integrationstest
muss mit `network` markiert und durch `DESINFECT_ALLOW_NETWORK_TESTS=1`
freigeschaltet werden.
