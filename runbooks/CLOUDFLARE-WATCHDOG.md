# Cloudflare-Watchdog: Deploy, Betrieb und Rollback

## Harte Grenze

Pull Requests und `main`-Pushes führen ausschließlich Tests, Typechecks und `wrangler deploy --dry-run` aus. Veröffentlichung ist nur per `workflow_dispatch` auf `main` möglich. Beide Deploy-Jobs verwenden geschützte GitHub-Environments; Repository-Secrets reichen nicht.

Production ist ausschließlich unter `https://production.workers.desinfect.telacore.org` erreichbar, Staging getrennt unter `https://staging.workers.desinfect.telacore.org`. `workers.dev` ist für beide Umgebungen deaktiviert; `wrangler.jsonc` ist die einzige Quelle dieses Routingvertrags.

GitHub-Gesamtausfall bleibt ein Retry-/Alarmfall. Worker und Durable Object ersetzen weder Repositorydaten noch Dispatcher. Öffentliche Requests sind bis auf `GET /healthz` immer `404`; Schreibendpunkte existieren nicht.

## Einmalige Einrichtung

1. Cloudflare-Worker-API-Token auf Account und Worker-Scripts begrenzen.
2. GitHub-Environment `cloudflare-watchdog-staging` anlegen, Required Reviewer aktivieren und `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_WATCHDOG_HEALTH_URL=https://staging.workers.desinfect.telacore.org/healthz` setzen. Zusätzlich nur als Environment-Secrets: `CLOUDFLARE_HEALTH_VPN_DE_CONFIG`, `CLOUDFLARE_HEALTH_VPN_NL_CONFIG`, `CLOUDFLARE_HEALTH_VPN_CH_CONFIG` und `CLOUDFLARE_HEALTH_VPN_AUTH`.
3. GitHub-Environment `cloudflare-watchdog-production` separat mit Required Reviewer und eigenen sieben Secrets anlegen; `CLOUDFLARE_WATCHDOG_HEALTH_URL` ist dort exakt `https://production.workers.desinfect.telacore.org/healthz`. Die vier `CLOUDFLARE_HEALTH_VPN_*`-Secrets müssen eigenständig je Environment gesetzt sein.
4. GitHub-App-Bindings `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID` und `GITHUB_APP_PRIVATE_KEY` gemäß `runbooks/CLOUDFLARE-GITHUB-AUTH.md` als Cloudflare-Secrets pro Worker einrichten. Werte nie in Workflow, Shellargument, Log oder Datei schreiben.
5. Beide Custom Domains in der Cloudflare-Zone `desinfect.telacore.org` prüfen. Wrangler verwaltet sie mit `custom_domain=true`; keine zusätzliche Worker-Route oder `workers.dev`-Adresse freigeben.
6. Durable-Object-Bindung `WATCHDOG_COORDINATOR` prüfen. Staging verwendet `desinfect-watchdog-staging` ohne Cron; Production verwendet `desinfect-watchdog` mit `0 2 * * *` UTC.

## Rollout

1. PR-Gates müssen grün sein.
2. Workflow `Cloudflare Watchdog` auf `main` manuell starten. `deploy_production=false` veröffentlicht nur Staging.
3. Workflow installiert OpenVPN bei Bedarf erst nach Deploy und prüft danach ausschließlich den öffentlichen `GET /healthz` über einen temporären Tunnel: DE, dann NL, dann CH. Die Wrapper-Konfiguration lässt nur benötigte OpenVPN-Direktiven samt eingebettetem `<ca>` zu, überschreibt Credentials und Gerät nichtinteraktiv und begrenzt jeden Client auf 45 Sekunden. Jede aufgelöste IPv4-/IPv6-Zieladresse wird geprüft; nur eine über `tun-health` geroutete Adresse wird ausgewählt, sonst scheitert der Check. Die Health-Prüfung bindet ihren Socket an `tun-health`, verbindet genau mit dieser Adresse und behält TLS-Hostnamen/SNI unverändert. Vor jedem Fallback beendet der Wrapper ausschließlich den verifizierten OpenVPN-PID mit TERM, wartet begrenzt, nutzt bei Bedarf KILL und reapet den Launcher; erst nach bestätigtem TUN-Abbau startet das nächste Land. PID, TUN, Log sowie temporäre Secret-Dateien werden auch bei Signal-Exit entfernt. `service`, `status` und `version` müssen exakt Workername, `ok` und aktuellem Git-SHA entsprechen.
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
