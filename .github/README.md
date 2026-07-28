# GitHub-Automatisierung

## P00-Bootstrap-Check

`p00-baseline.yml` ist ein bewusst schmaler, read-only Prüfworkflow für die Governance-Baseline. Er führt ausschließlich die P00-Validatoren und Offline-Unit-Tests aus.

- `permissions: contents: read`
- keine Secrets
- kein `pull_request_target`
- kein Deployment
- keine Repositoryschreiboperation
- Pull-Request-Prüfung, sobald GitHub den Workflow aus dem Default-Branch kennt
- verbindliche Post-Merge-Prüfung bei relevanten Pushes nach `main`
- `actions/checkout` ist auf einen vollständigen Commit-SHA gepinnt und in `config/actions-lock.json` registriert

Der Workflow wird in P11.1–P11.3 in die vollständige Validierungs-, Supply-Chain- und Required-Check-Infrastruktur überführt.
