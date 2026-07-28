# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 57 | 0 | 3 | 0 | 0 |

## Aktiver Durchlauf

**PR: #1** – `chore(plan): P00-Revisionsbaseline und ADR-Sperren`  
**Branch:** `agent/p00-governance-baseline`  
**Basis:** `main@fbcc6e850fec1f4592ca519fa3e5141b11a95e60`

| ID | Titel | Status | PR |
|---|---|---|---:|
| P00.1 | Revisionsblatt und Analysefreeze | im_review | #1 |
| P00.2 | Anforderungs- und Entscheidungstraceability | im_review | #1 |
| P00.3 | Fortschritts- und Evidenzvertrag | im_review | #1 |

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`. Merge-SHA, CI-Läufe und Abnahme werden erst nach den jeweiligen Ereignissen ergänzt; bis dahin bleiben die Plan-Checkboxen offen.
