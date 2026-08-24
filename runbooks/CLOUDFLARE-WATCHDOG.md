# Cloudflare-Watchdog: Deploy, Betrieb und Rollback

## Harte Grenze

Pull Requests und `main`-Pushes führen ausschließlich Tests, Typechecks und `wrangler deploy --dry-run` aus. Veröffentlichung ist nur per `workflow_dispatch` auf `main` möglich. Beide Deploy-Jobs verwenden geschützte GitHub-Environments; Repository-Secrets reichen nicht.

GitHub-Gesamtausfall bleibt ein Retry-/Alarmfall. Worker und Durable Object ersetzen weder Repositorydaten noch Dispatcher. Öffentliche Requests sind bis auf `GET /healthz` immer `404`; Schreibendpunkte existieren nicht.

## Einmalige Einrichtung

1. Cloudflare-Worker-API-Token auf Account und Worker-Scripts begrenzen.
2. GitHub-Environment `cloudflare-watchdog-staging` anlegen, Required Reviewer aktivieren und `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_WATCHDOG_HEALTH_URL` setzen.
3. GitHub-Environment `cloudflare-watchdog-production` separat mit Required Reviewer und eigenen drei Secrets anlegen.
4. GitHub-App-Bindings `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID` und `GITHUB_APP_PRIVATE_KEY` gemäß `runbooks/CLOUDFLARE-GITHUB-AUTH.md` als Cloudflare-Secrets pro Worker einrichten. Werte nie in Workflow, Shellargument, Log oder Datei schreiben.
5. Durable-Object-Bindung `WATCHDOG_COORDINATOR` prüfen. Staging verwendet `desinfect-watchdog-staging` ohne Cron; Production verwendet `desinfect-watchdog` mit `0 2 * * *` UTC.

## Rollout

1. PR-Gates müssen grün sein.
2. Workflow `Cloudflare Watchdog` auf `main` manuell starten. `deploy_production=false` veröffentlicht nur Staging.
3. Workflow prüft danach `GET /healthz` über HTTPS. `service`, `status` und `version` müssen exakt Workername, `ok` und aktuellem Git-SHA entsprechen.
4. Für Production mit `deploy_production=true` starten. Derselbe Lauf deployt und prüft zuerst Staging. Nur danach beginnt geschützter Production-Job; erst dieser aktiviert Cron aus kanonischer Wrangler-Konfiguration.
5. Nach erstem Cron-Lauf Cloudflare-Logs, DO-Alarm und GitHub-Dispatcher-Evidenz prüfen. Kein GitHub-Erfolg: gespeicherten Retry/Alarm untersuchen, nicht State oder Daten ersetzen.

## Rollback

Cron zuerst im Cloudflare-Dashboard deaktivieren. Deploymentliste prüfen und vorherige bekannte Version mit gelocktem CLI zurückrollen:

```bash
npm --prefix cloudflare/watchdog ci --ignore-scripts
npm --prefix cloudflare/watchdog exec -- wrangler rollback
```

Danach `/healthz` gegen erwartete Rollback-Version prüfen. DO-Zustand und Alarm vor manueller Löschung sichern; Secretreset darf Sperre nie umgehen. GitHub-Dispatcher bleibt unabhängig aktiv.

## Lokale und CI-Prüfung

```bash
python3 scripts/validate_cloudflare_config.py
npm --prefix cloudflare/watchdog test
npm --prefix cloudflare/watchdog run typecheck
npm --prefix cloudflare/watchdog run types:check
npm --prefix cloudflare/watchdog run deploy:dry-run
```
