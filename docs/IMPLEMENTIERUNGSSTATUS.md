# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 29 | 0 | 0 | 31 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |
| P03 | #7 | `e180b20788072bba840e655d493bac73c7f1a3ee` | Actions `30584133252`, CodeRabbit, qlty, 10 Reviewthreads |
| P04 | #10 | `b7148bb362425bc6f5a0d30b27a78539ec3acc75` | Actions `30665523318`, CodeRabbit, qlty, 19 Reviewthreads |
| P05 | #12 | `b1e6b0fa417b1ea879fe373795e320b1950970ba` | Actions `30784217751`, CodeRabbit, qlty, 10 Reviewthreads |
| P06 | #14–#17 | `c477be9`, `0e4fe01`, `54da096`, `71f8a58` | Actions `30798406250`, `30814619838`, `30831150430`, `30838403022`; CodeRabbit, qlty, 23 Reviewthreads |
| P07 | #19, #21, #23 | `cb378be`, `5cf540d`, `cf75685` | Actions und Nach-Merge-Gates erfolgreich; CodeRabbit, qlty, 15 Reviewthreads |
| P08 | #25, #27 | `6fccb8d`, `12ac906` | Actions und Nach-Merge-Gates erfolgreich; qlty, CodeRabbit-Feedback abgeschlossen, 4 Reviewthreads |
| P09.1 | #29 | `276e206381d9143547fddfc62cf51d6915cf046f` | Actions `30916701898`, Nach-Merge `30917009538`, CodeRabbit, qlty |
| P09.2 | #31 | `d02447cf5bd701dbdb961d22aa2d64609a69a369` | Actions `32776756666`, Nach-Merge `32777045027`, qlty, 3 CodeRabbit-Findings behoben |

## Abgeschlossene Phase P06

**PRs:** #14 bis #17

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P06.1 | Stabile Dokument-IDs, Pfade und Quellmanifest | umgesetzt | PR #14, Merge `c477be9ea08b`, Head `9c38fb5`, Actions `30798406250` |
| P06.2 | Rechte- und Lizenzpolicy | umgesetzt | PR #15, Merge `0e4fe01624d4`, Head `3b1d4bc`, Actions `30814619838` |
| P06.3 | PDF-Validierung und Konvertierung | umgesetzt | PR #16, Merge `54da0963f1b7`, Head `5a04bee`, Actions `30831150430` |
| P06.4 | Dokument-, Konvertierungs- und Storage-Manifeste | umgesetzt | PR #17, Merge `71f8a58b5c73`, Head `ce27bf1`, Actions `30838403022` |

## Abnahme P06

- GitHub Actions: vier PR-Läufe und Nach-Merge-Lauf `30838723565` erfolgreich
- CodeRabbit und qlty: auf allen vier PRs erfolgreich
- ungelöste Reviewthreads: 0
- aufgelöste Reviewthreads: 23
- Pytest: 782 Tests erfolgreich
- Unittest: 9 Tests erfolgreich
- Node: 2 Tests erfolgreich
- Ruff: `ruff check scripts tests` erfolgreich
- Security-Diff-Scan auf finalem P06.4-Head: 0 Findings, 31/31 Dateien abgedeckt
- Abgenommen am: `2026-08-03T17:54:15Z`
- Abgenommen durch: `H234598`

## Abnahme P09.2

- PR #31 per Squash mit geprüftem Head `a7f07af5e268af5588d97b6b013f26b200af66e8` gemergt
- Nach-Merge-Lauf `32777045027` erfolgreich
- ungelöste Reviewthreads: 0
- 1327 Pytest-, 9 Unittest-, 2 Node- und 23 Workers-Vitest-Tests erfolgreich
- TypeScript-, Wrangler-, Deploy-Dry-Run-, Lock- und Audit-Gates erfolgreich
- Abgenommen am: `2026-08-24T21:03:55Z`
- Abgenommen durch: `H234598`

## Nächster Schritt

P09.3 bleibt `offen`, bis DO-Sperre, Idempotenz, Alarm und Nachkontrolle implementiert, gemergt und remote abgenommen sind.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
