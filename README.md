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
- [Vorhandener RKI-Grabber](scripts/rki_grabber/RKI_EpidBull_Grabber_README.md)

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind verbindliche, automatisiert geprüfte Architekturentscheidungen.

## Governance-, P01- und P02-Baseline prüfen

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 -m scripts.rki_pipeline.runtime_status_cli --help
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

P01 legt Python 3.12+, Node 24/npm 11, exakt aufgelöste Python-Locks, sichere atomare Datei-/Stagingprimitive und einen kleinen Offline-Fixturekorpus fest.

P02 ergänzt zwölf strikte Draft-2020-12-Datenverträge, eine deterministische Statusmigration, getrennte Commit-/Lauf-/Schreibuhren, ein redigiertes Lauf-/Recoverymodell und eine deny-first Schreibpfad-Policy mit `@H234598` als initialem CODEOWNER.

Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.
