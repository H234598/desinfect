# Pipeline-Observability

## Zweck und Grenzen

P08.2 erzeugt redigierte GitHub-Jobzusammenfassungen, zeitlich begrenzte Diagnoseartefakte und optional genau ein Rolling Issue. Fachstatus und Workflowstatus bleiben getrennt: ein später fehlgeschlagener Gate- oder Commit-Schritt darf nicht durch ein zuvor erfolgreich abgeschlossenes Transaktionsmanifest verdeckt werden.

P08.2 aktiviert weder Cloudflare noch externe Dispatches. Ausführliche Diagnosen bleiben Actions-Artefakte und werden nicht ins Repository committed.

## Job Summary

`scripts.rki_pipeline.ci_summary` akzeptiert ein schema-valides RunManifest oder den Transaktionsumschlag mit `run_manifest`. Optional ergänzt `status.json` Wasserstände und Taxonomiestand. Ausgabe verwendet feste Überschriften und Feldnamen; fehlende Metriken erscheinen als `nicht gemeldet`.

```bash
python3 -m scripts.rki_pipeline.ci_summary \
  build/pipeline/transaction-result.json \
  --status status.json \
  --job-status success
```

Untrusted Text wird vor Markdownausgabe begrenzt, von ANSI-/Steuerzeichen befreit, redigiert und escaped. Tokens, Zugangsdaten, Credential-URLs und E-Mail-Adressen dürfen weder Summary noch Issue erreichen.

## Diagnoseartefakte und Retention

Der Pipelineworkflow rendert und lädt Diagnosen unter `if: always()` hoch. Diagnosefehler verändern den fachlichen Pipeline-Exit nicht. Retention richtet sich nach schwerstem sichtbaren Zustand:

- Erfolg, No-op oder Recovery: 14 Tage;
- Fehler oder Abbruch: 30 Tage;
- Blockade oder fehlendes/ungültiges Diagnosemanifest: 90 Tage.

Artefakte enthalten Dispatchplan, Transaktionsergebnis, Commitplan und generierte Job Summary, soweit vorhanden. Sie sind Evidenz, kein Recovery-Checkpoint und keine Quelle für einen erzwungenen Commit.

## Rolling Issue

Marker, Repository, Label und Titelpräfix sind fest:

- Marker: `<!-- desinfect:rki-pipeline-incident:v1 -->`
- Repository: `H234598/desinfect`
- Label: `pipeline-incident`

Ab konfigurierter Fehlerzahl wird das Marker-Issue erstellt, aktualisiert oder wieder geöffnet. Bei CI-Fehlern leitet `apply` die dauerhafte Fehlerfolge read-only aus abgeschlossenen Läufen desselben Caller-Workflows auf `main` ab; ein erfolgreicher Caller-Lauf setzt diese Folge zurück. Das eingebaute Actions-Token besitzt dafür nur `actions:read`. `apply` durchsucht alle Issues unabhängig vom Label, damit ein verlorenes Label kein Duplikat erzeugt. Vor einer Neuanlage prüft die Automatik das feste Label `pipeline-incident` und legt es bei Bedarf über die feste Repository-Route an. Nach Heilung folgt ein Kommentar und das offene Issue wird geschlossen. Mehr als ein Marker-Issue blockiert die Automatik; Duplikate werden nie still zusammengeführt.

Offline planen:

```bash
python3 -m scripts.rki_pipeline.incident_issue \
  --mode plan \
  --status status.json \
  --run-manifest build/pipeline/transaction-result.json \
  --job-status failure \
  --threshold 2
```

Der Offline-Plan fragt GitHub nicht ab und übernimmt deshalb keine Live-Issues in die Entscheidung.

`apply` benötigt `GH_TOKEN` aus einem kurzlebigen, repositorybegrenzten Wachhund-App-Token. Der Workflow fordert dafür separat nur `issues:write` an. Bei `--job-status failure` oder `--job-status cancelled` benötigt `apply` zusätzlich `ACTIONS_TOKEN` mit ausschließlich `actions:read` sowie die aktuelle Lauf-ID in `GITHUB_RUN_ID`. GitHub-Token, Repository, Label und Titel sind keine CLI- oder Payload-gesteuerten Freitextwerte.

## Aktivierung und Rücknahme

1. GitHub App `Wachhund` nur bei gewünschtem Rolling Issue um `Issues: write` ergänzen.
2. Repositoryvariable `ROLLING_ISSUE_ENABLED=true` setzen.
3. Optional `INCIDENT_FAILURE_THRESHOLD` setzen; Default ist `2`.
4. Job Summary und Planmodus prüfen, dann ersten Apply-Lauf beobachten.

Rücknahme: `ROLLING_ISSUE_ENABLED` entfernen oder auf einen anderen Wert setzen. Summary und Diagnoseartefakt bleiben aktiv; bestehendes Issue bleibt als Auditspur erhalten und kann manuell geschlossen werden. Keine Pipelineuhr oder Erfolgsmarke wird dabei verändert.
