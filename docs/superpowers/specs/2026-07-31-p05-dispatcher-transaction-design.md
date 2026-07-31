---
title: P05 – Dispatcher, Transaktion und GitHub-App-Writer – Design
aliases:
  - P05 Dispatcher Design
  - Transactional Writer Design
tags:
  - desinfect
  - p05
  - dispatcher
  - github-actions
  - github-app
  - transaction
  - catch-up
type: design
status: approved
created: 2026-07-31T21:30:00Z
date: 2026-07-31
---

# P05 – Dispatcher, Transaktion und GitHub-App-Writer – Design

## Auftrag und verbindliche Grundlagen

Dieses Design setzt die bereits freigegebene sequenzielle Phase P05 des kanonischen Implementierungsplans um:

- **P05.1:** Fälligkeitsberechnung und Catch-up
- **P05.2:** Transaktionaler Pipeline-Orchestrator
- **P05.3:** GitHub-App-Token, Commit und Push
- **P05.4:** GitHub-Workflows: Dispatcher, Pipeline und Backfill

Verbindliche Anforderungen:

- `MUSS-03`: Ein täglicher GitHub-Dispatcher berechnet fällige Aufgaben anhand von Wasserständen und Catch-up-Regeln.
- `MUSS-05`: Mehrere fällige Aufgaben laufen in einer Transaktion, einer Validierung und höchstens einem konsistenten Commit.
- `MUSS-06`: Gemeinsame Concurrency verhindert konkurrierende Repositoryschreiber.
- `V2-05-DISPATCH-001` bis `V2-05-DISPATCH-010`.
- `V2-14-GIT-001` bis `V2-14-GIT-007`.
- **ADR-003=A** und **ADR-014=B** bleiben unverändert.
- Die automatische Pfadpolicy bleibt deny-first.
- P05 führt keinen echten historischen RKI-Vollabruf ein; fachliche Dokumentkonvertierung und Archivbildung folgen in P06/P07.

## Bewertete Ansätze

### Ansatz A – Ein monolithischer geplanter Writer-Workflow

Ein einzelner Workflow berechnet Aufgaben, materialisiert Ergebnisse, validiert, committet und pusht.

**Vorteile:** wenig Workflow-YAML, einfache Übergabe lokaler Daten.

**Nachteile:** Dispatcherlogik, fachliche Pipeline und Schreibberechtigung sind vermischt; Tests und manuelle Backfills werden unnötig schwer; die Vertrauensgrenze des GitHub-App-Tokens beginnt zu früh.

### Ansatz B – Dispatcher plus wiederverwendbarer Pipeline-Workflow

Ein täglicher Dispatcher berechnet ausschließlich einen deterministischen Dispatchplan. Ein separater wiederverwendbarer Pipeline-Workflow erhält diesen Plan, führt eine Transaktion aus und erzeugt bei einer tatsächlichen Änderung höchstens einen Commit. Der manuelle Backfill-Workflow ruft denselben Pipeline-Workflow auf.

**Vorteile:** klare Vertrauensgrenzen, nur ein geplanter Einstieg, gemeinsame Concurrency, identische Transaktion für Tageslauf und Backfill, testbare Python-Kerne, GitHub-App-Token erst unmittelbar vor dem Write.

**Nachteile:** mehr Workflow-YAML und explizite Übergabeverträge.

### Ansatz C – Dispatcher löst einen zweiten Workflow über die GitHub API aus

Der Dispatcher erzeugt einen GitHub-App-Token und startet den Pipeline-Workflow per API.

**Vorteile:** vollständig getrennte Workflow-Runs.

**Nachteile:** zusätzliche API-Berechtigung, Race zwischen Planung und Ausführung, kompliziertere Idempotenz und Observability, Token bereits im Dispatcher erforderlich.

## Entscheidung

**Ansatz B wird umgesetzt.** Er erfüllt die Ein-Zeitplan-Invariante, minimiert die Berechtigungsdauer und ermöglicht eine gemeinsame, reproduzierbare Transaktion für reguläre Läufe und Backfills.

## Architektur

```text
schedule / workflow_dispatch
        |
        v
rki-dispatcher.yml (read-only)
        |
        | deterministischer DispatchPlan als Job-Output
        v
rki-pipeline.yml (workflow_call / manuell)
        |
        +--> Parse + validiere DispatchPlan
        +--> PipelineTransaction(plan)
        +--> PipelineTransaction(materialize) unter temp_root
        +--> genau eine globale Validierung
        +--> PipelineTransaction(apply)
        +--> staged WritePolicy-Validierung
        +--> CommitPlan / No-op
        +--> GitHub-App-Token erst jetzt
        +--> höchstens ein Commit + nicht-erzwungener Push nach main

rki-backfill.yml (nur workflow_dispatch)
        |
        +--> baut begrenzten DispatchPlan
        +--> ruft dieselbe rki-pipeline.yml auf
```

## Komponenten und Verantwortlichkeiten

### `scripts/rki_pipeline/due_tasks.py`

Reine Zeit- und Wasserstandslogik ohne Netzwerk, Git oder Dateischreibzugriffe.

Öffentliche Typen:

```python
class TaskKind(StrEnum):
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    RECONCILIATION = "reconciliation"

@dataclass(frozen=True, slots=True)
class DispatchLimits:
    max_weeks: int
    max_months: int
    max_years: int
    max_reconciliations: int

@dataclass(frozen=True, slots=True)
class DueTask:
    task_id: str
    kind: TaskKind
    period: str
    reason: str
    due_at: str
```

`calculate_due_tasks(status, now, limits)` berechnet nur abgeschlossene Zeiträume. Wochen verwenden ISO-Wochen, Monate `YYYY-MM`, Jahre Ganzzahlen als Text. Catch-up beginnt unmittelbar nach dem jeweiligen Wasserstand und ist pro Lauf begrenzt. Aufgaben werden deterministisch in dieser Reihenfolge sortiert:

1. Wochen, ältester Zeitraum zuerst;
2. Monate, ältester Zeitraum zuerst;
3. Jahre, ältester Zeitraum zuerst;
4. Reconciliation.

Ein fehlender Wasserstand beginnt nicht unkontrolliert im Jahr 1994, sondern erzeugt für den normalen Tageslauf ausschließlich den zuletzt abgeschlossenen Zeitraum. Historische Bereiche werden nur über den expliziten Backfill-Vertrag erzeugt.

Reconciliation wird fällig, wenn `last_reconciliation_at` fehlt oder mindestens 92 vollständige UTC-Tage zurückliegt.

### `scripts/rki_pipeline/dispatch_plan.py`

Definiert einen versionierten, kanonisch serialisierten Dispatchplan:

```python
@dataclass(frozen=True, slots=True)
class DispatchPlan:
    schema_version: str
    created_at: str
    trigger: str
    base_sha: str
    tasks: tuple[DueTask, ...]
    run_mode: RunMode
    storage_backend: StorageBackend
```

Der Plan besitzt einen SHA-256 über die kanonische JSON-Repräsentation. Parser und Konstruktor lehnen unbekannte Felder, doppelte `task_id`, unsortierte Aufgaben, unbekannte Modi, absolute Pfade und ungültige Zeitstempel ab.

### `scripts/rki_pipeline/dispatcher.py`

CLI- und API-Schicht zur Dispatchplanung. Sie lädt und validiert `status.json`, nimmt eine explizite UTC-Zeit und den beobachteten `main`-SHA entgegen und gibt den Dispatchplan ausschließlich nach stdout aus. Der tägliche Dispatcher schreibt keine Repositorydatei.

CLI:

```text
python -m scripts.rki_pipeline.dispatcher \
  --status status.json \
  --now 2026-07-31T21:30:00Z \
  --base-sha <40-hex> \
  --trigger schedule
```

Backfillbereiche werden über eine getrennte API `build_backfill_plan(...)` erzeugt. Sie verlangt `from_year`, `to_year` und eine harte maximale Anzahl von Aufgaben.

### `scripts/rki_pipeline/transaction.py`

Koordiniert eine vollständige Transaktion über vorhandene P04-Primitiven. Fachliche Arbeit wird über injizierte Handler ausgeführt; P05 selbst implementiert keine P06/P07-Konvertierung.

```python
class TaskHandler(Protocol):
    def plan(self, task: DueTask, context: TransactionContext) -> TaskPlan: ...
    def materialize(self, plan: TaskPlan, context: TransactionContext) -> TaskResult: ...
    def apply(self, result: TaskResult, context: TransactionContext) -> tuple[WriteOperation, ...]: ...

@dataclass(frozen=True, slots=True)
class TransactionResult:
    dispatch_plan_sha256: str
    tasks: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validation_count: int
    commit_required: bool
```

Ablauf:

1. Dispatchplan und beobachteten Basis-SHA prüfen.
2. Einen `run-manifest` mit allen Aufgaben erzeugen.
3. Alle Task-Pläne ohne Seiteneffekt erstellen.
4. Alle Task-Ergebnisse unter einem expliziten `temp_root` materialisieren.
5. Eine globale Validator-Pipeline genau einmal ausführen.
6. Alle Apply-Ergebnisse in einer gemeinsamen Arbeitskopie anwenden.
7. Tatsächliche Änderungen gegen die deny-first WritePolicy prüfen.
8. No-op oder einen CommitPlan erzeugen.
9. Run- und Statusprojektion erst als Bestandteil derselben Transaktion aktualisieren.

Bei irgendeinem Fehler vor dem Commit wird kein Push ausgeführt. Es gibt keinen Teilcommit pro Aufgabe.

### `scripts/rki_pipeline/commit_plan.py`

Erzeugt einen unveränderlichen Commitvertrag:

```python
@dataclass(frozen=True, slots=True)
class CommitPlan:
    expected_base_sha: str
    changed_paths: tuple[str, ...]
    subject: str
    body: str
    tree_sha256: str
```

Der `tree_sha256` wird aus sortierten Pfaden, Gitmodi und Blob-SHA-256 berechnet. Committexte enthalten keine externen RKI-Titel. Der Betreff ist stabil und kurz; der Body listet ausschließlich interne Task-IDs und den Dispatchplan-SHA.

### `scripts/rki_pipeline/git_writer.py`

Kapselt lokale Git-Aufrufe und ist vollständig über einen injizierbaren `GitRunner` testbar.

Sicherheitsabfolge:

1. Arbeitsbaum muss vor der Transaktion sauber sein.
2. `HEAD` muss `CommitPlan.expected_base_sha` entsprechen.
3. Nur explizit erlaubte Pfade werden gestaged.
4. `validate_index()` prüft jeden staged Pfad.
5. `git status --short` und `git diff --cached --name-status` werden diagnostisch ausgegeben.
6. Ein leerer staged Diff endet erfolgreich als No-op.
7. Der staged Baum wird erneut gegen `CommitPlan.tree_sha256` geprüft.
8. Es wird höchstens ein Commit erstellt.
9. Unmittelbar vor Push wird `origin/main` gefetcht und auf unveränderten Basis-SHA geprüft.
10. Push erfolgt ohne `--force` als `HEAD:main`.

### `scripts/validate_ci_mutation_safety.py`

Der bestehende Entwurf aus PR #8 wird in die aktuelle P05-Architektur übernommen, aktualisiert und durch strukturbezogene Workflowtests ergänzt. Jeder Writer-Schritt braucht No-op-Guard, Status-/Diff-Diagnostik und darf Audits nicht abschwächen. PR #8 wird nach Übernahme als superseded geschlossen.

### Workflows

#### `.github/workflows/rki-dispatcher.yml`

- Einziger `schedule`-Trigger des schreibenden Systems.
- Zusätzlich `workflow_dispatch` für einen normalen Tageslauf.
- `permissions: contents: read`.
- Berechnet den Dispatchplan read-only.
- Ruft `rki-pipeline.yml` nur bei mindestens einer Aufgabe auf.
- Gemeinsame Concurrency-Gruppe: `desinfect-repository-writer`.

#### `.github/workflows/rki-pipeline.yml`

- `workflow_call` und kontrolliertes `workflow_dispatch`.
- Gemeinsame Concurrency-Gruppe `desinfect-repository-writer`, `cancel-in-progress: false`.
- Checkout ohne persistierte Credentials.
- Vollständige plan/materialize/validate/apply-Transaktion.
- GitHub-App-Token wird erst nach erfolgreicher Validierung erzeugt.
- Token wird auf das aktuelle Repository und `contents: write` begrenzt.
- App-Variablen: `WACHHUND_APP_CLIENT_ID`; Secret: `WACHHUND_APP_PRIVATE_KEY`.
- Fehlende App-Konfiguration blockiert mit klarer Diagnose, statt auf `GITHUB_TOKEN` zurückzufallen.

#### `.github/workflows/rki-backfill.yml`

- Ausschließlich `workflow_dispatch`.
- Pflichtparameter `from_year`, `to_year`, `max_tasks`, `run_mode`.
- Kein Zeitplan.
- Ruft dieselbe Pipeline auf und teilt dieselbe Concurrency.
- `apply` benötigt zusätzlich die Eingabe `confirm_apply=APPLY`.

## Datenfluss und Wasserstände

`status.json` bleibt die öffentliche Betriebsakte. P05 liest:

- `periods.last_completed_week`
- `periods.last_completed_month`
- `periods.last_completed_year`
- `periods.last_reconciliation_at`
- `pipeline.last_successful_run_at`
- `pipeline.last_successful_write_at`

Wasserstände werden nur nach vollständig erfolgreicher Apply-/Verify-Phase fortgeschrieben. Ein fehlgeschlagener Lauf darf keine Periode als abgeschlossen markieren. `last_successful_run_at` und `last_successful_write_at` bleiben getrennt.

## Fehler- und Wiederholungsverhalten

- Ungültiger Status oder Dispatchplan: fail-closed vor Materialisierung.
- Geänderter `main`-SHA zwischen Planung und Transaktion: blockiert und retryable.
- Geänderter `main`-SHA unmittelbar vor Push: kein Push, keine Force-Operation, retryable.
- Leerer Dispatchplan: erfolgreicher No-op ohne GitHub-App-Token.
- Leerer staged Diff: erfolgreicher No-op ohne Commit und Push.
- Validierungsfehler: kein Commit; Diagnose wird redigiert im Run-Manifest erfasst.
- Mehrere fällige Aufgaben: alle oder keine; ein gemeinsamer Commit.
- Wiederholung desselben Dispatchplans: idempotenter No-op, sofern der Zielzustand bereits vorliegt.

## Teststrategie

### Reine Unit-Tests

- ISO-Wochen-, Monats- und Jahresgrenzen, einschließlich Schaltjahr und Jahreswechsel.
- Catch-up-Limits, fehlende Wasserstände und deterministische Sortierung.
- Dispatchplan-Schema, Hashstabilität und Duplikatabwehr.
- CommitPlan-Hersteller und Tree-Hash.

### Transaktionstests

- mehrere Aufgaben → genau eine Validierung und höchstens ein CommitPlan;
- Fehler in Aufgabe 2 → kein Apply/Commit für Aufgabe 1;
- No-op aller Aufgaben → kein Commit;
- verbotener Pfad → Abbruch vor Indexmutation;
- Statuswasserstände werden nur nach erfolgreicher Verifikation fortgeschrieben.

### Git-Integrationstests in lokalen temporären Repositories

- sauberer Commit/No-op;
- zusätzlicher staged Pfad blockiert;
- divergierendes `origin/main` blockiert;
- kein Force-Push;
- höchstens ein neuer Commit;
- Commit enthält exakt registrierte Pfade.

### Workflow-Vertragstests

- genau ein `schedule` im gesamten Repository;
- alle drei Writer-Workflows teilen dieselbe Concurrency;
- Backfill besitzt keinen Schedule;
- Pipeline erzeugt App-Token erst nach Validatoren;
- kein Fallback auf schreibenden `GITHUB_TOKEN`;
- Checkout persistiert keine Credentials;
- mutierende Schritte erfüllen Variante B.

## Nichtumfang von P05

- keine produktive Dokumentkonvertierung;
- keine Wochen-/Monats-/Jahres-ZIP-Erzeugung;
- kein historischer Vollabruf;
- kein externer Cloudflare-Wächter;
- keine Rechteentscheidung;
- keine Websitepublikation.

P05 liefert die sichere Scheduling-, Transaktions- und Commit-Infrastruktur, auf der die späteren fachlichen Phasen aufbauen.
