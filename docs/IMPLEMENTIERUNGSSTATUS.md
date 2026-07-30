# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 44 | 0 | 4 | 12 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |
| P03 | #7 | `e180b20788072bba840e655d493bac73c7f1a3ee` | Actions `30584133252`, CodeRabbit, qlty, 10 Reviewthreads |

## Aktive Phase P04

**PR: #10**  
**Branch:** `agent/p04-runmodes-storage`  
**Basis:** `main@762eb90a5be858e34abfbad63492f300b215cbeb`

| ID | Titel | Status | Evidenzstand |
|---|---|---|---|
| P04.1 | RunMode und Seiteneffektwächter | im_review | PR #10; Modusmatrix, EffectLedger und Git-/Status-/TempRoot-Snapshots implementiert |
| P04.2 | Storage Protocol und echte Adapter | im_review | PR #10; strikte Konfiguration, backendneutrale Referenzen und LFS-/Release-/Object-Adapter implementiert |
| P04.3 | Git-LFS-Tracking, Objekt- und Budgetprüfung | im_review | PR #10; Tracking-, Pointer-, Objekt- und Budgetgates implementiert |
| P04.4 | Backend-Migrationswerkzeug | im_review | PR #10; deterministische `copy|unchanged|conflict`-Migration und lokale Drill-CLI implementiert |

### Aktueller Gate-Stand

- GitHub Actions `30586996977`: erfolgreich
- Pytest: 126 Tests erfolgreich
- Unittest, Compile, Node und P00–P04-Validatoren: erfolgreich
- PR bleibt bis zum Abschluss von CodeRabbit, qlty und Reviewthreads offen.
- Es wurden keine echten Release-, Object-Storage-, LFS-Transfer-, Commit-, Push- oder Scheduler-Seiteneffekte ausgeführt.

Die P04-Pakete bleiben bis zum tatsächlichen Merge, grüner finaler CI, aufgelösten Reviewthreads und eingetragener Abnahmeevidenz offen.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
