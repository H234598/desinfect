# Provenienzregister

## P00 – Governance-Baseline

Die in diesem PR neu angelegten Register, Validatoren und Statusprojektionen sind Eigenentwicklungen auf Grundlage des bereitgestellten Implementierungsplans.

### Eingefrorene Referenzen

- `H234598/desinfect@fbcc6e850fec1f4592ca519fa3e5141b11a95e60`
- `H234598/ADHS-Lernpfad@93c8c02d263ec123c1c271caf0d2deaa76760ccb`
- `H234598/Cheatsheets@7db8f713aca07e67b481f9fbcb00553f6a555495`

Der am 28. Juli 2026 beobachtete Cheatsheets-HEAD `69c72997eed4fc0ac831eba696bac12b3a2f69b9` ist dokumentierte Drift und wird nicht still zur neuen Übernahmequelle.

### Kanonische Planquelle

Der vollständige bereitgestellte Plan besitzt SHA-256 `aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4` und exakt `533417` Bytes. `config/plan-source.json` friert Dateiname, Größe und Fingerabdruck ein.

Die kanonisch im Repository gepflegte Ausführungssteuerung ist `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`. Deren Bytehash wird separat registriert und von der Baseline bei jedem Lauf neu berechnet. Dadurch können Ursprungsprovenienz und fortgeschriebener Umsetzungsstand nicht unbemerkt auseinanderlaufen.

Die gesperrten Entscheidungen bleiben unabhängig von sprachlichen Präzisierungen ihrer sicheren Ausführung unverändert: **ADR-003=A** und **ADR-014=B**.
