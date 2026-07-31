# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 44 | 0 | 0 | 16 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |
| P03 | #7 | `e180b20788072bba840e655d493bac73c7f1a3ee` | Actions `30584133252`, CodeRabbit, qlty, 10 Reviewthreads |
| P04 | #10 | `b7148bb362425bc6f5a0d30b27a78539ec3acc75` | Actions `30665523318`, CodeRabbit, qlty, 19 Reviewthreads |

## Abgeschlossene Phase P04

**PR:** #10 — `feat(p04): RunModes, Storage und Git LFS`  
**Merge:** `b7148bb362425bc6f5a0d30b27a78539ec3acc75`  
**Geprüfter Head:** `62ef771edaba96d5e3212d1525164367d3e46dbe`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P04.1 | RunMode und Seiteneffektwächter | umgesetzt | PR #10, Merge `b7148bb36242`, Actions `30665523318` |
| P04.2 | Storage Protocol und echte Adapter | umgesetzt | PR #10, Merge `b7148bb36242`, Actions `30665523318` |
| P04.3 | Git-LFS-Tracking, Objekt- und Budgetprüfung | umgesetzt | PR #10, Merge `b7148bb36242`, Actions `30665523318` |
| P04.4 | Backend-Migrationswerkzeug | umgesetzt | PR #10, Merge `b7148bb36242`, Actions `30665523318` |

## Abnahme P04

- GitHub Actions: erfolgreich
- CodeRabbit: erfolgreich
- qlty: erfolgreich
- ungelöste Reviewthreads: 0
- behobene Reviewthreads: 19
- Pytest: 171 Tests erfolgreich
- Unittest: 9 Tests erfolgreich
- Node: 2 Tests erfolgreich
- Abgenommen am: `2026-07-31T21:13:46Z`
- Abgenommen durch: `H234598`
- Es wurden keine echten Release-, Object-Storage-, Git-LFS-Netzwerktransfers, Bot-Commits, Pushes oder Scheduler in P04 ausgeführt.

## Nächster Schritt

P05.1 bis P05.4 bleiben `offen`, bis ihr eigener Implementierungsbranch und PR vorliegen. Die Phase führt Fälligkeitsberechnung und Catch-up, den transaktionalen Pipeline-Orchestrator, GitHub-App-Token mit Commit/Push sowie Dispatcher-, Pipeline- und Backfill-Workflows ein.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
