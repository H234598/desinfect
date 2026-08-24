# Externer Wachhund

Dieses Verzeichnis enthält Worker, Singleton-Durable-Object, feste GitHub-App-Operationen und P09.3-State-Machine. Ohne vollständige GitHub-App-Bindings bleibt der Cron lokal und führt keinen Netzwerkaufruf aus. Produktiver Deploy und Secret-Einrichtung folgen in P09.4.

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

`wrangler.jsonc` enthält nur nicht geheime Konfiguration. Der private GitHub-App-Schlüssel wird ausschließlich als Cloudflare-Secret gebunden:

```bash
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

Secretwert nie in Shellargument, Log, Datei oder Git schreiben. Laufzeit benötigt zusätzlich `GITHUB_APP_ID` und `GITHUB_INSTALLATION_ID`; P09.4 richtet alle drei Bindings kontrolliert ein.

## P09.3 State und Nachkontrolle

- `blockConcurrencyWhile` initialisiert `watchdog-state-v1` mit festem Schema.
- Schlüssel bindet Git-Blob-SHA von `status.json`, fälliges Bark-Fenster, festen Workflow und festen Taskset.
- Reservierung wird vor Recovery persistiert; laufender oder unbestätigter Schlüssel blockiert jeden weiteren Dispatch.
- Nach Dispatch folgt ein Alarm nach sechs Stunden. Fehlende oder temporär nicht lesbare Runs erhalten höchstens drei gespeicherte Postchecks mit begrenztem Backoff; niemals Redispatch.
- Malformed Status, falsches Repository, Intervall-Drift, ungültiger SHA und unbekannte State-Version scheitern geschlossen.

## Feste GitHub-API-Grenzen

Implementiert sind:

- RSA-SHA256 JWT für die GitHub-App im Worker-Runtime.
- Installationstoken mit festen Rechten (`actions: write`, `contents: read`) und Laufzeitlimit.
- feste GitHub-REST-Endpunkte nur für `H234598/desinfect` und `rki-dispatcher.yml`:
  - `/repos/H234598/desinfect/contents/status.json?ref=main`
  - `/repos/H234598/desinfect/commits?sha=main&per_page=1`
  - `/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml`
  - `/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/enable`
  - `/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/dispatches`
  - `/repos/H234598/desinfect/actions/workflows/rki-dispatcher.yml/runs?branch=main&event=workflow_dispatch&per_page=<maxCount>`
- feste API-Headers (`Accept`, `Authorization`, `User-Agent`, `X-GitHub-Api-Version`), fixe Fehlergrenzen und no-trace/No-Storage-Handling.

## Runbooks

- `runbooks/CLOUDFLARE-GITHUB-AUTH.md`: Secrets, Tokenfluss, Schwellwerte und manuelle App/Installation-Basis.
- `docs/Wartung/Cloudflare-Waechter.md`: State, Alarme, Degraded Mode und Rückbau.

Aktuelle Primärreferenzen:

- <https://developers.cloudflare.com/workers/configuration/cron-triggers/>
- <https://developers.cloudflare.com/durable-objects/reference/durable-objects-migrations/>
- <https://developers.cloudflare.com/workers/testing/vitest-integration/>
