# Cloudflare – GitHub-App-Bindung für externen Watchdog

## Geheimnisse

- `GITHUB_APP_ID` (decimal string)
- `GITHUB_APP_INSTALLATION_ID` (decimal string)
- `GITHUB_APP_PRIVATE_KEY` (PEM private key)

`GITHUB_APP_PRIVATE_KEY` wird als Cloudflare-Secret gesetzt:

```bash
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

`GITHUB_APP_ID` und `GITHUB_APP_INSTALLATION_ID` sind bei GitHub als feste Ziel-IDs vorgesehen.
Sie können als Worker-Secrets oder feste Vars geführt werden.

## API-Vertrag

- `POST /app/installations/{installation_id}/access_tokens`
  - Body: `{"repositories":["desinfect"],"permissions":{"actions":"write","contents":"read"}}`
  - Header:
    - `Authorization: Bearer <app_jwt>`
    - `Accept: application/vnd.github+json`
    - `X-GitHub-Api-Version: 2026-03-10`
    - `User-Agent: desinfect-watchdog`
- `GET /repos/H234598/desinfect/contents/status.json?ref=main`
- `GET /repos/H234598/desinfect/commits?sha=main&per_page=1`
- `GET /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml`
- `PUT /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/enable`
- `POST /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/dispatches`
  - Body: `{"ref":"main"}`
- `GET /repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/runs?branch=main&event=workflow_dispatch&per_page=<max>`

## Fehler und Grenzen

- Antworten werden auf begrenzte Größe gelesen.
- Nicht erfolgreiche Antworten führen zu `GithubApiError` mit Retry-Flag bei:
  - HTTP `429`
  - HTTP `5xx`
  - `403` bei `x-ratelimit-remaining: 0`
- Token/Werte werden nicht geloggt und nicht persistent gespeichert.

## P09.2-Verhalten

- workflow `active` → no-op.
- workflow `disabled_inactivity` → enable + dispatch.
- andere Zustände: fail-closed Fehler.

## P09.3 Schnittstellen

P09.3 ergänzt DO-Lock, Cooldown, Idempotenzschlüssel, Alarm und Dispatch-Postcheck.
