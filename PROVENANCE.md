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

Die gesperrten Entscheidungen bleiben unabhängig von diesen Fundamentarbeiten unverändert: **ADR-003=A** und **ADR-014=B**.
