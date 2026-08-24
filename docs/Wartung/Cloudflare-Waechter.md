# Cloudflare-Wächter: State, Alarme und Rückbau

## Zweck und Grenze

Der externe Wächter prüft ausschließlich `H234598/desinfect`, `status.json` auf `main` und `rki-dispatcher.yml`. Öffentliche HTTP-Schreibpfade existieren nicht. GitHub-Zugriffe beginnen nur bei vollständigen App-Bindings; produktive Einrichtung und Deploy gehören zu P09.4.

## Persistenter Zustand

Singleton `desinfect-watchdog` speichert unter `watchdog-state-v1` Schema `1`:

- aktueller Idempotenzschlüssel und Phase `pending`, `dispatched`, `verified` oder `failed`;
- Status-Blob-SHA und fälliges `next_bark_at`;
- Start-/Dispatchzeit und Anzahl der Nachkontrollen;
- Cooldown- und Alarmzeit.

Initialisierung läuft in `blockConcurrencyWhile`. Fehlende Daten werden einmalig angelegt. Beschädigter Zustand oder unbekannte Schemaversion stoppt geschlossen; keine automatische verlustbehaftete Migration.

## Ablauf

1. Cooldown und unbestätigte Operation werden vor neuer Tokenanforderung geprüft.
2. `status.json` muss festen Repositorynamen, konfiguriertes 45-Tage-Intervall, gültigen UTC-Zeitpunkt und 40-stelligen Git-Blob-SHA liefern.
3. Bei Fälligkeit reserviert der DO den Schlüssel vor jedem Recovery-Aufruf.
4. Nur `disabled_inactivity` führt über feste P09.2-Endpunkte zu Enable + Dispatch. Aktiver Workflow bleibt No-op.
5. Dispatch setzt einen Alarm nach sechs Stunden. Run-Nachkontrolle bestätigt Erfolg oder plant begrenzten Backoff. Es gibt keinen Redispatch aus Alarm- oder Fehlerpfaden.

## Idempotenz und At-least-once-Zustellung

Cloudflare Cron und Alarme können mehrfach zugestellt werden. Der DO serialisiert Instanzarbeit und persistiert vor externer Mutation. Gleicher Schlüssel sowie jede noch `pending`/`dispatched` laufende Operation blockieren weitere Dispatches, auch nach Neustart oder geändertem Status-SHA. Unsicherheit bevorzugt ausgelassenen Dispatch gegenüber möglichem Duplikat.

## Degraded Mode und Rückbau

- GitHub nicht erreichbar: gespeicherte Nachkontrolle mit begrenztem Backoff; danach `failed`.
- App-Binding fehlt: keine Netzwerkaktion.
- Malformed Status/State: Fehler, keine Mutation auf GitHub.
- Rückbau: Cron deaktivieren, vorhandenen DO-Alarm löschen, Zustand erst nach Evidenz sichern und dann kontrolliert entfernen. Secretreset darf eine Sperre nie umgehen.
- GitHub-Dispatcher bleibt unabhängig und kann manuell betrieben werden.

## Lokale Prüfung

```bash
npm --prefix cloudflare/watchdog test -- watchdog-state
npm --prefix cloudflare/watchdog run typecheck
npm --prefix cloudflare/watchdog run types:check
npm --prefix cloudflare/watchdog run deploy:dry-run
```
