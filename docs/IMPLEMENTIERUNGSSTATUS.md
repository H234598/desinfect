# Implementierungsstatus

> [!important]
> Ein Arbeitspaket wird erst nach Merge, grüner CI und vollständiger Evidenz als `umgesetzt` geführt.

## Zusammenfassung

| Gesamt | Offen | In Arbeit | Im Review | Umgesetzt | Blockiert |
|---:|---:|---:|---:|---:|---:|
| 60 | 54 | 3 | 0 | 3 | 0 |

## Abgeschlossene Phase P00

**PR:** #1 — `chore(plan): P00-Revisionsbaseline und ADR-Sperren`  
**Merge:** `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`  
**Geprüfter Head:** `c61581842415da4d5c2e81ae7e28fbe7f4165a8f`

| ID | Titel | Status | Evidenz |
|---|---|---|---|
| P00.1 | Revisionsblatt und Analysefreeze | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |
| P00.2 | Anforderungs- und Entscheidungstraceability | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |
| P00.3 | Fortschritts- und Evidenzvertrag | umgesetzt | PR #1, Merge `c4996105f6d6`, Actions `30331599906` |

## Aktiver Durchlauf P01

**Branch:** `agent/p01-foundation`  
**Basis:** `main@68f1c73d043abd4a778cf3ee0dfa3cf857330efe`

| ID | Titel | Status |
|---|---|---|
| P01.1 | Python-/Node-Paketfundament | in_arbeit |
| P01.2 | Sichere Datei-, Hash- und Stagingprimitive | in_arbeit |
| P01.3 | Offline-Fixtures und Testdatenpolicy | in_arbeit |

Geplante blockierende Prüfungen:

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

Die P01-Pakete decken `V2-03-TREE-001` bis `V2-03-TREE-005` und `V2-26-TEST-001` bis `V2-26-TEST-008` ab. Merge-, CI- und Abnahmeevidenz stehen noch aus.

## Gesperrte Architekturentscheidungen

- **ADR-003 = A**
- **ADR-014 = B**

Die kanonische Maschinenquelle ist `docs/implementation-status.json`.
