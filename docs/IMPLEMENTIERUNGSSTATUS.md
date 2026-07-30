# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 48 | 0 | 0 | 12 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |
| P03 | #7 | `e180b20788072bba840e655d493bac73c7f1a3ee` | Actions `30584133252`, CodeRabbit, qlty, 10 Reviewthreads |

## Abgeschlossene Phase P03

**PR:** #7 — `feat(p03): RKI-Grabber modularisieren und härten`  
**Merge:** `e180b20788072bba840e655d493bac73c7f1a3ee`  
**Geprüfter Head:** `71943a05fe0f6a2a013f1794e64601b32a44d079`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P03.1 | Grabber in Parser, HTTP und Orchestrierung schneiden | umgesetzt | PR #7, Merge `e180b2078807`, Actions `30584133252` |
| P03.2 | Netzwerk-, Robots- und Downloadhärtung | umgesetzt | PR #7, Merge `e180b2078807`, Actions `30584133252` |
| P03.3 | Stabile CLI, API und Resultvertrag | umgesetzt | PR #7, Merge `e180b2078807`, Actions `30584133252` |

## Abnahme P03

- GitHub Actions: erfolgreich
- CodeRabbit: erfolgreich
- qlty: erfolgreich
- ungelöste Reviewthreads: 0
- behobene Reviewthreads: 10
- Pytest: 90 Tests erfolgreich
- Abgenommen am: `2026-07-30T21:45:22Z`
- Abgenommen durch: `H234598`
- Kein echter RKI-Abruf, kein historischer Backfill, kein LFS-Import und kein produktiver Writer wurden in P03 ausgeführt.

## Nächster Schritt

P04.1 bis P04.4 bleiben `offen`, bis ihr eigener Implementierungsbranch und PR vorliegen. Die Phase führt RunModes, Seiteneffektwächter, Storage-Adapter, Git LFS und Backendmigration ein.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
