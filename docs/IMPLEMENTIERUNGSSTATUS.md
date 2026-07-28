# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 51 | 3 | 0 | 6 | 0 |

## Abgeschlossene Phase P00

**PR:** #1 — `chore(plan): P00-Revisionsbaseline und ADR-Sperren`  
**Merge:** `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`  
**Geprüfter Head:** `c61581842415da4d5c2e81ae7e28fbe7f4165a8f`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P00.1 | Revisionsblatt und Analysefreeze | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |
| P00.2 | Anforderungs- und Entscheidungstraceability | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |
| P00.3 | Fortschritts- und Evidenzvertrag | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |

## Abgeschlossene Phase P01

**PR:** #3 — `feat(p01): Paket-, IO- und Offline-Testfundament`  
**Merge:** `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2`  
**Geprüfter Head:** `a0b6d26ee91ba0e1c6d531c6b7fd2ab6058393aa`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P01.1 | Python-/Node-Paketfundament | umgesetzt | PR #3, Merge `4fc4aca667ce`, Actions `30336885794` |
| P01.2 | Sichere Datei-, Hash- und Stagingprimitive | umgesetzt | PR #3, Merge `4fc4aca667ce`, Actions `30336885794` |
| P01.3 | Offline-Fixtures und Testdatenpolicy | umgesetzt | PR #3, Merge `4fc4aca667ce`, Actions `30336885794` |

## Aktive Phase P02

**Branch:** `agent/p02-data-contracts`

| ID | Titel | Status | Evidenzstand |
|---|---|---|---|
| P02.1 | Schemafamilie und Versionsstrategie | in_arbeit | zwölf strikte Schemas, Registry und Statusmigration implementiert |
| P02.2 | Öffentlicher Status und Lauf-/Recovery-Modell | in_arbeit | Statusprojektion, Zustandsautomat, Revision, Redaction und CLI implementiert |
| P02.3 | Automatische Schreibpfad-Policy | in_arbeit | deny-first Policy, Indexprüfung und CODEOWNERS implementiert |

Die P02-Pakete bleiben bis zum tatsächlichen Merge, grüner CI, aufgelösten Reviewthreads und eingetragener Abnahmeevidenz offen.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
