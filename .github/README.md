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
