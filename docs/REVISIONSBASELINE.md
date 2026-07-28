# Revisionsbaseline

Stand: 28. Juli 2026, 05:12 Uhr Europe/Berlin

| Repository | Rolle | Plan-Freeze | beobachteter `main`-HEAD | Drift |
|---|---|---|---|---|
| `H234598/desinfect` | Ziel | `fbcc6e850fec1f4592ca519fa3e5141b11a95e60` | `fbcc6e850fec1f4592ca519fa3e5141b11a95e60` | unverändert |
| `H234598/ADHS-Lernpfad` | Laufstatus/Recovery/Web | `93c8c02d263ec123c1c271caf0d2deaa76760ccb` | `93c8c02d263ec123c1c271caf0d2deaa76760ccb` | unverändert |
| `H234598/Cheatsheets` | sichere IO/Content/Build | `7db8f713aca07e67b481f9fbcb00553f6a555495` | `69c72997eed4fc0ac831eba696bac12b3a2f69b9` | nach Freeze fortgeschritten |

Der neuere Cheatsheets-Stand wird nicht still übernommen. Jede Referenzaktualisierung benötigt einen eigenen Provenienz-Diff, erneute Tests und einen reviewten PR. Nicht lesbare Einstellungen sind in `config/reference-revisions.json` als manuell zu verifizieren markiert.

## Gesperrte Entscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Prüfung: `python scripts/validate_baseline.py`.
