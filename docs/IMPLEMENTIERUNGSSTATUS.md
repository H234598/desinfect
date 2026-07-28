# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 48 | 3 | 0 | 9 | 0 |

## Abgeschlossene Phasen

| Phase | Implementierungs-PR | Merge | Gate |
|---|---:|---|---|
| P00 | #1 | `c4996105f6d683c2c4d342df6ee43b74dbcb64a6` | Actions `30331599906`, CodeRabbit, qlty |
| P01 | #3 | `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2` | Actions `30336885794`, CodeRabbit, qlty |
| P02 | #5 | `947b2ba86792d5a84e0f2fd972cfbe554c156afc` | Actions `30342761383`, CodeRabbit, qlty, 9 Reviewthreads |

## Aktive Phase P03

**Branch:** `agent/p03-grabber-modularization`

| ID | Titel | Status | Evidenzstand |
|---|---|---|---|
| P03.1 | Grabber in Parser, HTTP und Orchestrierung schneiden | in_arbeit | reine Parser, Transportport und Service implementiert; Offline-Fixtures ergänzt |
| P03.2 | Netzwerk-, Robots- und Downloadhärtung | in_arbeit | same-origin HTTPS, fail-closed Robots, Redirect-/Bytegrenzen und atomarer Resume-Download implementiert |
| P03.3 | Stabile CLI, API und Resultvertrag | in_arbeit | gemeinsame API/CLI, Result-Schema, stabile Exitcodes und Validator implementiert |

P03 bleibt bis Implementierungs-PR, grüner CI, aufgelösten Reviewthreads, Merge und separater Abnahmeevidenz offen. In dieser Phase erfolgt kein echter RKI-Abruf und kein produktiver Schreib- oder LFS-Lauf.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
