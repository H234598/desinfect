# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI, aufgelösten Reviewthreads und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 24 | 0 | 0 | 35 | 1 |

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
| P09.3 | #33 | `accec2b4e5cbb92970b4157641fce69c7bcb10c5` | Actions `32781301922`, Nach-Merge `32781564903`, CodeRabbit approved, qlty, 2 Reviewthreads |
| P10.1 | #37 | `f1e8c1bc72ec80648855a39f9b5c304bc909ff7a` | Actions `32799650475`, `32799650531`; Nach-Merge `32800771394`, `32800771392`; CodeRabbit approved, qlty, 4 Reviewthreads |
| P10.2 | #42 | `996e8973e5720ac6f5b93b13c2e15d6f5c47bec1` | Actions `32838827613`, Cloudflare-Validierung `32838827676`; Nach-Merge `32839553105`, `32839553069`; CodeRabbit approved, qlty |
| P10.3 | #45, #46, #49–#51 | `79a5e91967af9af89167c3c085acd362ad455aaf` | Nach-Merge `32901411769`, `32901411756`; CodeRabbit current-head success, qlty status success, 0 offene Reviewthreads |

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

## Abnahme P09.3

- PR #33 per Squash mit geprüftem Head `c37f864a9cbc1bfc48a536ad0aabf108641bb64f` gemergt
- Nach-Merge-Lauf `32781564903` erfolgreich
- ungelöste Reviewthreads: 0
- 1327 Pytest-, 9 Unittest-, 2 Node- und 36 Workers-Vitest-Tests erfolgreich
- TypeScript-, Wrangler-, Deploy-Dry-Run- und Audit-Gates erfolgreich
- Abgenommen am: `2026-08-24T21:52:43Z`
- Abgenommen durch: `H234598`

## Blockierter Implementierungsstand P09.4

- P09.4 ist nicht umgesetzt; Staging-Freigabe, nachfolgende Production-Freigabe und produktive Runtimeabnahme fehlen.
- PR #35 wurde mit geprüftem Head `d72b0a12c950c3beb2552af9909c66b975cb2cfa` als `f9ce544e8b094db3d919ae920c989214e25bac86` gemergt.
- PR-Gates: Cloudflare `32786594070`, Baseline `32786594072`, CodeRabbit approved und qlty erfolgreich.
- Nach-Merge-Gates: Cloudflare `32786895489` und Baseline `32786895469` erfolgreich.
- Custom-Domain-Fix PR #38 wurde mit geprüftem Head `2056e60a7c8333c503fe45d0916305f703f54112` als `4d1acd9c252db838c1a78223c138cf4d1d2363f2` gemergt; Actions `32799960155` und `32799960202`, CodeRabbit approved und qlty erfolgreich.
- Staging läuft über `staging.workers.desinfect.telacore.org`, Production über `production.workers.desinfect.telacore.org`; `workers.dev` ist deaktiviert.
- `cloudflare-watchdog-staging` und `cloudflare-watchdog-production` besitzen Required Reviewer `H234598` sowie die benannten Secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` und `CLOUDFLARE_WATCHDOG_HEALTH_URL`.
- Workflow `32800551483`: Worker-Validierung erfolgreich; `Deploy and verify staging` wartet am manuellen Environment-Gate. Production wurde noch nicht ausgerollt oder abgenommen.
- Keine Abnahmezeit und keine abnehmende Person eingetragen.

## Abnahme P10.1

- PR #37 per Squash mit geprüftem Head `8b0ddf0e59bc810756ada95df232a56112a99f1f` als `f1e8c1bc72ec80648855a39f9b5c304bc909ff7a` gemergt
- PR-Läufe `32799650475` und `32799650531` sowie Nach-Merge-Läufe `32800771394` und `32800771392` erfolgreich
- CodeRabbit approved, qlty erfolgreich, ungelöste Reviewthreads: 0, aufgelöste Reviewthreads: 4
- Finale PR-CI: 1465 Pytest- und 9 Unittest-Tests erfolgreich
- P10.1-Suite auf Merge-Stand: 123 Tests erfolgreich
- Closeout-Gesamtsuite auf Merge-Stand: 1469 Pytest-Tests erfolgreich
- Baseline-, Planfortschritts-, Planquellen-, Ruff-, Format- und Diff-Prüfungen erfolgreich
- Abgenommen am: `2026-08-25T02:16:17Z`
- Abgenommen durch: `H234598`

## Abnahme P10.2

- PR #42 per Squash mit geprüftem Head `e7cb213fd2f5ce34bc9e92b03cc2bdbd98b3ba3a` als `996e8973e5720ac6f5b93b13c2e15d6f5c47bec1` gemergt
- PR-Gates: Baseline `32838827613`, Cloudflare-Validierung `32838827676`, CodeRabbit APPROVED und qlty erfolgreich
- Nach-Merge-Gates: Baseline `32839553105` und Cloudflare-Validierung `32839553069` erfolgreich
- Cloudflare-Läufe validierten nur den Worker; Staging- und Production-Deploy-Jobs wurden übersprungen. Kein echter Staging-Deploy ist als grün abgenommen.
- Lokale Verifikation: 139 Build-, 262 Web- und 1632 Pytest-Tests erfolgreich
- Unabhängige Abschlussprüfung: keine Critical/Important Findings; Ready to merge: yes
- Baseline-, Planfortschritts-, Planquellen-, Ruff-, Format- und Diff-Prüfungen erfolgreich
- Abgenommen am: `2026-08-25T10:54:11Z`
- Abgenommen durch: `H234598`

## Abnahme P10.3

- PR #45 lieferte Navigation und Grundstruktur: Head `40c9008ccba160ecfcfeaf3b167c3b0128d532ac`, Merge `ad0d1b2a070c3fb3e24eab19c11983b45b2914fd`
- PR #46 lieferte fail-closed Tabellendaten und vollständiges serverseitiges No-JS-Rendering: Head `962479fc73078ad4fd53a3bb4abb97f9b779b0b7`, Merge `0ec2e42b039d69c239b6def4dd82b8c5b1f03ba6`
- PR #49 lieferte progressive Sortier- und Filterverbesserung ohne neue Datenquelle: Head `322f5d2d46e6a7f349cc70bfbede8d93ffd3e88d`, Merge `7340ccc93e5799b82655b3ca2291aab21e82c9bc`
- PR #50 lieferte responsive UX, Accessibility-Verträge und Betriebsdokumentation: Head `5873fef19146f7e391021190c15ba69b8648e2b2`, Merge `8f32222ce99cdde880f46521ca1c73bf53165933`
- PR #51 korrigierte die in Task 5 entdeckte Partial-Preview-Projektion fail-closed: Head `7074ba719ab56a0ec522e8ee64bbc2446a19946e`, Merge `79a5e91967af9af89167c3c085acd362ad455aaf`
- PR-Checks #51: Baseline `32900391676`, Cloudflare-Validierung `32900391762`, CodeRabbit current-head success, qlty status success und 0 ungelöste aktuelle Reviewthreads
- Nach-Merge-Checks: Baseline `32901411769` und Cloudflare-Validierung `32901411756` erfolgreich; Deploy-Jobs waren nicht Bestandteil dieser Abnahme
- Lokale Merge-SHA-Verifikation: 1833 Pytest-, 9 Unittest-, 12 Node- und 37 Worker-Tests erfolgreich; npm audit ohne Vulnerabilities; Typecheck, Wrangler-Staging-Dry-Run, vollständiger Strict-Build und Teilpreviews für `index.md` und `Tabelle.md` erfolgreich
- Zwei unabhängige Abschlussreviews: 0 Critical, 0 Important, 0 Minor; Ready to merge: yes
- Qlty-Grenze: Status erfolgreich, aber keine Behauptung „0 Findings“. Cloud-Originalreport zu PR #50 war login-only; lokale Rekonstruktion ergab 24 Gesamt- und 4 neue test-only Style-/Refactor-Findings. PR #51 zeigte trotz erfolgreichem Status den projektweiten Linktext „44 blocking issues“.
- Keine echte Browser-, Axe- oder 390px-Abnahme durchgeführt; diese bleibt ausdrücklich P11.4
- Abgenommen am: `2026-08-25T21:36:54Z`
- Abgenommen durch: `H234598`

## Nächster Schritt

P10.4 ist nächster interner Schritt. P11.4 bleibt für echte Browser-, Axe- und 390px-Smokes `offen`. P09.4 bleibt bis zur Staging-Freigabe, nachfolgenden Production-Freigabe und produktiven Runtimeabnahme `blockiert`.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
