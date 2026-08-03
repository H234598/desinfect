# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 40 | 0 | 0 | 20 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |
| P03 | #7 | `e180b20788072bba840e655d493bac73c7f1a3ee` | Actions `30584133252`, CodeRabbit, qlty, 10 Reviewthreads |
| P04 | #10 | `b7148bb362425bc6f5a0d30b27a78539ec3acc75` | Actions `30665523318`, CodeRabbit, qlty, 19 Reviewthreads |
| P05 | #12 | `b1e6b0fa417b1ea879fe373795e320b1950970ba` | Actions `30784217751`, CodeRabbit, qlty, 10 Reviewthreads |

## Abgeschlossene Phase P05

**PR:** #12 — `feat(p05): Dispatcher, Transaktion und GitHub-App-Writer`

**Merge:** `b1e6b0fa417b1ea879fe373795e320b1950970ba`

**Geprüfter Head:** `d9e1c5b39cc7fb714ba61d089119bfa5b81c080b`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P05.1 | Fälligkeitsberechnung und Catch-up | umgesetzt | PR #12, Merge `b1e6b0fa417b`, Actions `30784217751` |
| P05.2 | Transaktionaler Pipeline-Orchestrator | umgesetzt | PR #12, Merge `b1e6b0fa417b`, Actions `30784217751` |
| P05.3 | GitHub-App-Token, Commit und Push | umgesetzt | PR #12, Merge `b1e6b0fa417b`, Actions `30784217751` |
| P05.4 | GitHub-Workflows: Dispatcher, Pipeline und Backfill | umgesetzt | PR #12, Merge `b1e6b0fa417b`, Actions `30784217751` |

## Abnahme P05

- GitHub Actions: erfolgreich
- CodeRabbit: erfolgreich
- qlty: erfolgreich
- ungelöste Reviewthreads: 0
- behobene Reviewthreads: 10
- Pytest: 249 Tests erfolgreich
- Unittest: 9 Tests erfolgreich
- Node: 2 Tests erfolgreich
- Abgenommen am: `2026-08-03T04:29:27Z`
- Abgenommen durch: `H234598`
- PR #8 wurde nach Replacement-Kommentar `5162329352` am `2026-08-03T04:32:36Z` geschlossen und nicht gemergt.

## Nächster Schritt

P06.1 bis P06.4 bleiben `offen`, bis ihr eigener Implementierungsbranch und PR vorliegen. Die Phase führt stabile Dokument-IDs, Pfade und Quellmanifeste, Rechte- und Lizenzpolicy, PDF-Validierung und Konvertierung sowie Dokument-, Konvertierungs- und Storage-Manifeste ein.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
