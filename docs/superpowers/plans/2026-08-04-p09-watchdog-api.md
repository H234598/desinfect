# P09.2 GitHub-API-Fundament – Umsetzungsplan

> Stand: 4. August 2026, autonome Phase

## Ziel

GitHub-App-JWT + feste GitHub-REST-Endpunkte für den externen Watchdog vorbereiten, ohne P09.3-Orchestrierung zu implementieren.

## Tasks

- [x] `cloudflare/watchdog/src/github-app.ts` für RS256-JWT-Erzeugung.
- [x] `cloudflare/watchdog/src/github-api.ts` für feste Endpunkte:
  - `POST /app/installations/{installation_id}/access_tokens`
  - `GET /repos/{owner}/{repo}/contents/status.json?ref=main`
  - `GET /repos/{owner}/{repo}/commits?sha=main&per_page=1`
  - `GET /repos/{owner}/{repo}/actions/workflows/{workflow_file}`
  - `PUT /actions/workflows/{workflow_file}/enable`
  - `POST /actions/workflows/{workflow_file}/dispatches`
  - `GET /actions/workflows/{workflow_file}/runs?...`
- [x] `cloudflare/watchdog/test/github-api.test.ts` mit no-op-/disabled_inactivity-Flows und Endpoint-Assertions.
- [x] README- und Runbook-Doku ergänzt.
- [ ] `cloudflare/watchdog/src/index.ts` für diesen Schritt unverändert lassen (weiterhin no-op Foundation).

## Grenzen

- Keine DO-Lock- oder Idempotenzlogik in P09.2.
- Keine Dauerzustandsspeicherung von Tokens.
