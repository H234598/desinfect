# Externer Wachhund – Projektfundament

Dieses Verzeichnis enthält das lokale Cloudflare-Worker-/Durable-Object-Fundament für P09.1. Es führt noch keinen GitHub-Aufruf und keinen Dispatch aus.

## Lokale Gates

```bash
npm ci --ignore-scripts
npm test
npm run typecheck
npm run types:check
npm run deploy:dry-run
npm audit --audit-level=high
```

`deploy:dry-run` baut und validiert lokal. Es veröffentlicht keinen Worker.

## Feste Laufzeitgrenzen

- Cron: täglich `02:00 UTC`; Cron Trigger laufen laut Cloudflare in UTC.
- Durable Object: genau `desinfect-watchdog` über Binding `WATCHDOG_COORDINATOR`.
- Storage: SQLite über deklaratives Wrangler-`exports`.
- HTTP: jeder Request erhält `404`; es gibt keinen öffentlichen Schreibendpunkt.
- Ziel: ausschließlich `H234598/desinfect` und `rki-dispatcher.yml`.
- Intervalle: `WATCHDOG_INTERVAL_DAYS=45`, `WATCHDOG_GRACE_HOURS=12`, `DISPATCH_COOLDOWN_HOURS=24`; Runtime validiert sichere Grenzen.

## Secrets

`wrangler.jsonc` enthält nur nicht geheime Konfiguration. P09.2 bindet den privaten GitHub-App-Schlüssel ausschließlich als Cloudflare-Secret:

```bash
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

Secretwert nie in Shellargument, Log, Datei oder Git schreiben. App- und Installation-ID folgen ebenfalls erst mit P09.2.

## Abgrenzung

P09.1 enthält absichtlich keine JWT-, GitHub-REST-, Dispatch-, Idempotenz-, Alarm- oder Deploylogik. Rückbau: `cloudflare/watchdog/` und zugehörige CI-Zeilen entfernen; GitHub-Pipeline bleibt unabhängig.

## P09.2 Vorschau

P09.2 ergänzt:

- RSA-SHA256 JWT für die GitHub-App im Worker-Runtime.
- Installationstoken mit festen Rechten (`actions: write`, `contents: read`) und Laufzeitlimit.
- feste GitHub-REST-Endpunkte nur für `H234598/desinfect` und `rki-dispatcher.yml`:
  - `contents/status.json?ref=main`
  - `commits?sha=main&per_page=1`
  - `actions/workflows/{workflow_file}`
  - `.../{workflow_file}/enable`
  - `.../{workflow_file}/dispatches`
  - `.../{workflow_file}/runs?branch=main&event=workflow_dispatch&per_page=<fix>`
- feste API-Headers (`Accept`, `Authorization`, `User-Agent`, `X-GitHub-Api-Version`), fixe Fehlergrenzen und no-trace/No-Storage-Handling.

## Runbooks

- `runbooks/CLOUDFLARE-GITHUB-AUTH.md`: Secrets, Tokenfluss, Schwellwerte und manuelle App/Installation-Basis.

Aktuelle Primärreferenzen:

- <https://developers.cloudflare.com/workers/configuration/cron-triggers/>
- <https://developers.cloudflare.com/durable-objects/reference/durable-objects-migrations/>
- <https://developers.cloudflare.com/workers/testing/vitest-integration/>
