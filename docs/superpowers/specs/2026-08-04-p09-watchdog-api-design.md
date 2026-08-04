# P09.2 Watchdog GitHub-API-Foundation Design

> 4. August 2026

## Entscheidung

Variant A: fester GitHub-App-JWT-Fluss mit RS256, Repository-/Workflow-Bindung auf `H234598/desinfect` und `rki-dispatcher.yml`, keine dynamische Route. Keine DO-Idempotenz/Alarme in P09.2.

## Contracts

- JWT (`RS256`, short-lived): `iat = now - 60s`, `exp = iat + 540s`, `iss = appId`.
- Installationstoken:
  - Endpoint: `/app/installations/{id}/access_tokens`
  - Body: `{"repositories":["desinfect"],"permissions":{"actions":"write","contents":"read"}}`
  - Kein Caching, kein Persistenzfluss.
- Endpunkte:
  - `GET /repos/H234598/desinfect/contents/status.json?ref=main`
  - `GET /repos/H234598/desinfect/commits?sha=main&per_page=1`
  - `GET /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml`
  - `PUT /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/enable`
  - `POST /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/dispatches`
  - `GET /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/runs?branch=main&event=workflow_dispatch&per_page=...`
- Recovery-Contract:
  - Workflow `active` → no-op.
  - `disabled_inactivity` → enable + dispatch.
- Fehler:
  - Retryable bei 429/5xx oder `403` mit `x-ratelimit-remaining: 0`.
  - Fehlertext cap + Tokenredaktion.
