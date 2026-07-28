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

## Aktueller Evidenzstand

- Die externe Planlangfassung ist durch die Größe `533417` Bytes und SHA-256 `aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4` eingefroren; der Bytehash der kanonischen Steuerungsdatei wird aus `config/plan-source.json` verifiziert.
- Der letzte vollständig grüne Vor-Reparaturstand war `952c59154ce6a5f84bfcea5da19a077726e7cef5` mit GitHub-Actions-Lauf `30327108111`, CodeRabbit `success` und qlty `success`.
- Die noch offenen Reviewbefunde werden im selben PR behoben; der finale Reparaturhead benötigt anschließend erneut grüne Checks und aufgelöste Threads.
- P00 bleibt bis zum tatsächlichen Merge und der nachgelagerten Abnahmeevidenz vollständig `im_review`.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`. Merge-SHA, finale CI-Läufe und Abnahme werden erst nach den jeweiligen Ereignissen ergänzt; bis dahin bleiben die Plan-Checkboxen offen.
