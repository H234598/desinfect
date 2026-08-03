# Provenienzregister

## P00 – Governance-Baseline

Die in PR #1 angelegten Register, Validatoren und Statusprojektionen sind Eigenentwicklungen auf Grundlage des bereitgestellten Implementierungsplans.

### Eingefrorene Referenzen

- `H234598/desinfect@fbcc6e850fec1f4592ca519fa3e5141b11a95e60`
- `H234598/ADHS-Lernpfad@93c8c02d263ec123c1c271caf0d2deaa76760ccb`
- `H234598/Cheatsheets@7db8f713aca07e67b481f9fbcb00553f6a555495`

Der am 28. Juli 2026 beobachtete Cheatsheets-HEAD `69c72997eed4fc0ac831eba696bac12b3a2f69b9` ist dokumentierte Drift und wird nicht still zur neuen Übernahmequelle.

### Kanonische Planquelle

Der vollständige bereitgestellte Plan besitzt SHA-256 `aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4` und exakt `533417` Bytes. `config/plan-source.json` friert Dateiname, Größe und Fingerabdruck ein.

Die kanonisch im Repository gepflegte Ausführungssteuerung ist `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`. Deren Bytehash wird separat registriert und von der Baseline bei jedem Lauf neu berechnet.

## P01 – Paket-, IO- und Fixturefundament

### Angepasste Übernahme

- Quelle: `H234598/Cheatsheets@scripts/io_utils.py`
- Commit: `7db8f713aca07e67b481f9fbcb00553f6a555495`
- Blob: `28c388e9e36d3642168dfa9cb3a40075cf027dda`
- Ziele: `scripts/rki_pipeline/io_utils.py`, `scripts/rki_pipeline/staging.py`
- Anpassungen: Sentinel `.desinfect-generated-root`, NFC-/POSIX-Normalisierung, Symlink- und portable Kollisionsprüfung, Parent-Verzeichnis-`fsync`, gleiches Dateisystem und Fault-Injection-Rollback.

Das vorhandene `.part`-/`os.replace`-Muster aus `H234598/desinfect@fbcc6e850fec1f4592ca519fa3e5141b11a95e60` bleibt als fachliche Herkunft ebenfalls dokumentiert. Paketvalidatoren, Offline-Fixtures und P01-Tests sind Eigenentwicklungen auf Grundlage des Plans.

## P02 – Datenverträge, Status und Schreibgrenzen

- Das Laufstatus- und Recoverymodell ist konzeptionell aus `H234598/ADHS-Lernpfad@93c8c02d263ec123c1c271caf0d2deaa76760ccb` (`automation/run-status.schema.json`, `scripts/automation_status.py`, `scripts/runtime_status_cli.py`) abgeleitet und für `desinfect` neu implementiert.
- Pfadnormalisierung, atomare Schreibgrenzen und Kollisionsprüfung verwenden die in P01 dokumentierte Cheatsheets-Provenienz.
- Die zwölf Domänenschemas, das Migrationsregister und die deny-first Schreibpolicy sind Eigenentwicklungen nach dem bereitgestellten Implementierungsplan.

## P03 – Modularer und gehärteter RKI-Grabber

- Fachliche Quelle: `H234598/desinfect@fbcc6e850fec1f4592ca519fa3e5141b11a95e60`, `scripts/rki_grabber/rki_epidbull_grabber.py`, Blob `808ab02f24b4bbf3a6ad7d88c61a03a68c846cb8`.
- Übernommen und modularisiert: Handle-/Jahres-/Datumsparser, DSpace-Pagination, Metadaten-/DOI-/MD5-Erkennung, serielles Delay, Retry- und Resumeabsicht, CSV-/JSONL-Kompatibilitätsausgaben.
- Neu gehärtet: importfreundliche Abhängigkeitsgrenze, same-origin HTTPS und manuelle Redirectkontrolle, fail-closed Robotsvertrag, Antwort-/PDF-Größenlimits, `%PDF-`/`%%EOF`, descriptor-relative atomare Ablage, strikter Grabber-Resultvertrag, stabile Exitcodes und vollständige Offline-Ports/Fixtures.
- Die P03-Parser-, Transport-, Download-, API- und Schema-Tests sind Eigenentwicklungen auf Grundlage des eingefrorenen Plans und des ursprünglichen Grabbers.

## P04 – RunModes, Storage und Backendmigration

- `scripts/rki_pipeline/run_modes.py`, das EffectLedger, die Git-/Status-/TempRoot-Snapshots und die Modusmatrix sind Eigenentwicklungen nach den MUSS-17/18-Vorgaben des eingefrorenen Plans.
- Das Storage Protocol, die backendneutrale `StorageReference`, die strikte `config/storage.toml` und die Adapterfactory sind Eigenentwicklungen nach MUSS-13/15/16/34.
- Das Git-LFS-Pointerformat und der Objektpfad `.git/lfs/objects/<aa>/<bb>/<oid>` folgen der öffentlichen Git-LFS-Spezifikation; Parser, Trackingregeln, Integritätsprüfung und Budgetpolicy wurden repositoryspezifisch neu implementiert.
- Release- und Object-Adapter verwenden ausschließlich eigene, injizierbare Ports. Es wurde kein Code aus Cloud- oder GitHub-SDKs übernommen und in P04 kein echter Remotezugriff ausgeführt.
- Die idempotente Migration `copy|unchanged|conflict`, das nicht destruktive Quellverhalten, die LFS-Drill-CLI und sämtliche P04-Tests sind Eigenentwicklungen nach dem freigegebenen Implementierungsplan.
- Der damalige Draft-PR #8 wurde in P04 nur als Kontext für spätere CI-Schreibpfade geprüft; P04 übernahm daraus keine Implementierung und führte keinen produktiven Writer ein. Die erst in P05 erfolgte angepasste Integration ist unten ausgewiesen.

## P05 – Dispatcher, Transaktion und GitHub-App-Writer

- Die Variante-B-Konzepte für mutationssichere CI-Schreibschritte stammen aus dem von `H234598` erstellten Draft-PR #8 „CI: Variante-B-Vertrag für spätere Schreibpfade“, Branch `agent/ci-variant-b-20260730`, Head `8abf3b0071046ecd3ce3bc4547c63b69a5286fac`. Diese Quelle und Urheberschaft bleiben für die in P05/PR #12 angepasst integrierten Teile maßgeblich; der Draft wurde nicht unverändert gemergt.
- P05 übertrug den Vertrag auf die aktuellen Workflows `rki-dispatcher.yml`, `rki-pipeline.yml` und `rki-backfill.yml` und ergänzte strukturiert ausgewertete YAML-Workflowtests. Die Integration behandelt einen leeren staged Diff als No-op, protokolliert `git status --short` und den staged `--name-status`-Diff und lässt Audit- sowie Mutation-Safety-Gates blockierend.

## P06 – Identität, Rechte, Konvertierung und Manifeste

- Dokument-/Bitstream-Identitäten, Pfadregeln, Rechtepolicy, PDF-Härtung, Conversion-Fingerprints und Manifestgraph wurden repositoryspezifisch nach dem freigegebenen Implementierungsplan neu implementiert.
- Poppler-Programme werden ausschließlich als extern installierte, versionsgebundene Laufzeitwerkzeuge aufgerufen; Quellcode wurde nicht übernommen. OCR bleibt bis zur expliziten Laufzeitfreigabe fail-closed.
- Katalogformat, strikter Loader, Rechte-/Storage-Autorisierung und atomare Publikation verwenden Eigenimplementierungen auf Python-Standardbibliothek und bestehenden Repositoryprimitiven.

Die gesperrten Entscheidungen bleiben unabhängig von diesen Arbeiten unverändert: **ADR-003=A** und **ADR-014=B**.
