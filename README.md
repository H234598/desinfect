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
- [RunModes und Storage](docs/Wartung/RunModes-und-Storage.md)
- [Python-API](docs/API.md)
- [CLI-Dokumentation](scripts/rki_grabber/RKI_EpidBull_Grabber_README.md)

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind verbindliche, automatisiert geprüfte Architekturentscheidungen.

## Governance- und P01–P06-Baseline prüfen

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 scripts/validate_p04_storage.py
python3 scripts/validate_p05_dispatcher.py
python3 scripts/validate_rights_register.py
python3 scripts/validate_manifests.py --root tests/fixtures/manifests
python3 scripts/validate_ci_mutation_safety.py
python3 -m scripts.rki_pipeline.runtime_status_cli --help
python3 -m scripts.rki_grabber.rki_epidbull_grabber --help
python3 -m scripts.rki_pipeline.storage_cli --help
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

P01 legt Python 3.12+, Node 24/npm 11, exakt aufgelöste Python-Locks, sichere atomare Datei-/Stagingprimitive und einen kleinen Offline-Fixturekorpus fest.

P02 ergänzt zwölf strikte Draft-2020-12-Datenverträge, eine deterministische Statusmigration, getrennte Commit-/Lauf-/Schreibuhren, ein redigiertes Lauf-/Recoverymodell und eine deny-first Schreibpfad-Policy mit `@H234598` als initialem CODEOWNER.

P03 bewahrt die fachliche Grabberbasis, trennt Parser, HTTP, Download und Orchestrierung, führt eine gemeinsame importierbare API/CLI sowie den strikten `grabber-result`-Vertrag ein und härtet die RKI-Trust-Boundary. P03 startet keinen echten RKI-Abruf.

P04 trennt `plan|materialize|apply`, überwacht Repository-/Index-/Status- und TempRoot-Effekte, führt ein echtes `lfs|release|object` Storage Protocol, Git-LFS-Objekt-/Budgetgates und eine idempotente, nicht destruktive Backendmigration ein. Remoteadapter bleiben in P04 hinter injizierten Offline-Ports.

P05 führt einen täglichen UTC-Dispatcher mit begrenztem Catch-up und einen manuellen Backfill ein. Alle fälligen Aufgaben durchlaufen eine gemeinsame Transaktion und Validierung; nur eine tatsächliche, geprüfte Änderung erzeugt höchstens einen Commit über die repository-begrenzte GitHub App `Wachhund`. Einrichtung und Betrieb beschreiben [Automatische Schreibpfade](docs/Wartung/Automatische-Schreibpfade.md) sowie [Status und Recovery](docs/Wartung/Status-und-Recovery.md).

P06 führt stabile RKI-Dokument- und Bitstream-Identitäten, kanonische Ablagepfade, fail-closed Rechteentscheidungen, gehärtete PDF-Validierung, deterministische Markdown-Konvertierung und atomar publizierte Manifestkataloge ein. Öffentliche oder interne Persistenz bleibt an aktuelle Rechte- und Storage-Entscheidungen gebunden.

Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.
