# GitHub-Automatisierung

## P00/P01/P02/P03-Baseline-Check

`p00-baseline.yml` ist ein read-only Prüfworkflow für Governance, Paket-/IO-Fundament, P02-Datenverträge und den modularen P03-RKI-Grabber.

- `permissions: contents: read`
- keine Secrets
- kein `pull_request_target`
- kein Deployment
- keine Repositoryschreiboperation
- Pull-Request- und relevante `main`-Prüfung
- Python 3.12 über vollständig gepinntes `actions/setup-python`
- Node 24 über vollständig gepinntes `actions/setup-node`
- `actions/checkout`, `setup-python`, `setup-node` und `upload-artifact` sind mit vollständigen Commit-SHAs in `config/actions-lock.json` registriert
- direkte Python-Absichten werden per frischem Resolverbericht mit den kanonischen transitiven Lockdateien verglichen
- Installation ausschließlich aus den geprüften Locks beziehungsweise mit `npm ci --ignore-scripts`
- Governance-, Fixture-, IO-/Staging-, Schema-, Status-, Migration-, Schreibpolicy-, Parser-, HTTP-, Download-, API-, Python- und Node-Tests laufen blockierend
- die P03-Tests verwenden ausschließlich manifestierte Offline-Fixtures und injizierte Fake-Transporte
- `.github/CODEOWNERS` führt `@H234598` als initialen globalen und expliziten Infrastruktur-CODEOWNER

Der Workflow bleibt bis P11 bewusst schmal und wird dort in die vollständige Validierungs-, Diagnose-, Supply-Chain- und Required-Check-Infrastruktur überführt. P03 führt weder einen echten RKI-Abruf noch einen schreibenden Workflow ein.

## P08.2 Pipeline-Observability

Der wiederverwendbare Pipelineworkflow rendert unter `if: always()` eine redigierte Job Summary und lädt vorhandene Transaktionsevidenz zeitlich begrenzt hoch. Workflowstatus und Transaktionsstatus bleiben getrennt; Diagnose- oder Artefaktfehler verändern den fachlichen Exit nicht.

Ein Rolling Issue ist standardmäßig aus. Nur `ROLLING_ISSUE_ENABLED=true` aktiviert einen getrennten, kurzlebigen Wachhund-App-Token mit `issues:write` für `H234598/desinfect`. `INCIDENT_FAILURE_THRESHOLD` steuert die Schwelle, Default `2`. Marker, Label und Titelpräfix sind fest; Duplikate blockieren statt ein weiteres Issue zu erzeugen. Details: `docs/Wartung/Observability.md` und `runbooks/PIPELINE-FAILED.md`.
