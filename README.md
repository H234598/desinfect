# desinfect

`desinfect` wird zu einer reproduzierbaren Archiv-, Konvertierungs-, Überwachungs- und Publikationspipeline für RKI-Epidemiologische Bulletins ausgebaut.

## Umsetzung

- [Revisionsbaseline](docs/REVISIONSBASELINE.md)
- [Implementierungsstatus](docs/IMPLEMENTIERUNGSSTATUS.md)
- [Steuerungsplan](docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md)
- [Architecture Decision Records](docs/adr/README.md)
- [Anforderungsregister](docs/requirements/README.md)
- [Datenverträge](docs/Wartung/Datenvertraege.md)
- [Status und Recovery](docs/Wartung/Status-und-Recovery.md)
- [Automatische Schreibpfade](docs/Wartung/Automatische-Schreibpfade.md)
- [Modularer RKI-Grabber](docs/Wartung/RKI-Grabber.md)
- [Python-API](docs/API.md)
- [CLI-Dokumentation](scripts/rki_grabber/RKI_EpidBull_Grabber_README.md)

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind verbindliche, automatisiert geprüfte Architekturentscheidungen.

## Governance-, P01-, P02- und P03-Baseline prüfen

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 -m scripts.rki_pipeline.runtime_status_cli --help
python3 -m scripts.rki_grabber.rki_epidbull_grabber --help
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

P01 legt Python 3.12+, Node 24/npm 11, exakt aufgelöste Python-Locks, sichere atomare Datei-/Stagingprimitive und einen kleinen Offline-Fixturekorpus fest.

P02 ergänzt zwölf strikte Draft-2020-12-Datenverträge, eine deterministische Statusmigration, getrennte Commit-/Lauf-/Schreibuhren, ein redigiertes Lauf-/Recoverymodell und eine deny-first Schreibpfad-Policy mit `@H234598` als initialem CODEOWNER.

P03 bewahrt die fachliche Grabberbasis, trennt Parser, HTTP, Download und Orchestrierung, führt eine gemeinsame importierbare API/CLI sowie den strikten `grabber-result`-Vertrag ein und härtet die RKI-Trust-Boundary. P03 startet keinen echten RKI-Abruf.

Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.
