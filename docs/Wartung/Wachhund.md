# Interner Wachhund

## Zweck und Grenzen

P08.1 plant interne Bark-Statusupdates. Es erzeugt weder Git-Commits noch Issues oder Netzwerkzugriffe. P08.2 übernimmt Diagnoseausgaben und Rolling Issues; P09 implementiert den unabhängigen Cloudflare-Wächter.

## Getrennte Uhren

`pipeline.last_main_commit_at`, `pipeline.last_successful_run_at` und `pipeline.last_successful_write_at` bleiben unabhängig. Ein Bark aktualisiert keinen dieser Werte. Fehlgeschlagene Läufe und reine Bark-Updates gelten nicht als erfolgreicher Lauf oder Schreiblauf.

## Fälligkeit und Cooldown

Das in `status.json` persistierte `watchdog.interval_days` muss zwischen 7 und 55 liegen und ist für P08.1 maßgeblich; Reset-Default ist 45. `last_reset_at` und `next_bark_at` werden gemeinsam gesetzt. Vor `next_bark_at` entsteht kein Plan. Ab Fälligkeit entsteht genau ein Plan; dessen Projektion setzt `next_bark_at` auf Auswertungszeit plus Intervall. Dadurch entstehen weder tägliche Wiederholungen noch nachgeholte Bark-Bursts.

## Reset

Nur ein erfolgreicher oder wiederhergestellter `apply`-Lauf mit tatsächlich erzeugtem, beabsichtigtem Repository-Commit darf `reset_watchdog` aufrufen. Plan-, Materialize-, No-op- und Fehlerläufe setzen nicht zurück. Resetgrund bleibt in `reset_by`; `last_bark_at` bleibt als Auditspur erhalten.

## Planung

```bash
python -m scripts.rki_pipeline.cli watchdog \
  --as-of 2026-09-04T00:00:00Z \
  --mode plan \
  --status status.json
```

Ausgabe ist deterministisches JSON. `due=false` enthält keinen Barkplan. `due=true` enthält Ursache, Wiederholungsstatus, Committext und nächste Fälligkeit, führt aber keine Änderung aus.

## Committext

Fehlt oder veraltet `last_successful_run_at` oder `last_successful_write_at`, lautet der Titel `chore(wachhund): <N> Tage ohne erfolgreichen Schreiblauf erkannt`. Andernfalls – also bei planmäßigem Keepalive oder ausschließlich fehlendem/veraltetem `last_main_commit_at` – lautet er `chore(wachhund): neues Betriebsupdate nach <N> Tagen Inaktivität`.

Der stabile Body enthält Auswertungszeit, internen Trigger `internal-watchdog`, Intervall, die drei getrennten Pipeline-Uhren, Ursachen, Wiederholungsflag und nächste Fälligkeit.

## Fehler und Recovery

Ungültige Intervalle, Zeitstempel, teilweise initialisierte Zustände, Zukunftsuhren und widersprüchliche Deadlines blockieren. Status bleibt unverändert. Ursache korrigieren und denselben Planbefehl erneut ausführen. Einen veralteten Barkplan nie erzwingen; gegen aktuellen Status neu planen.

## Benutzeraktion

Für P08.1 ist keine Repositoryvariable zu setzen. Der persistierte Statuswert ist maßgeblich; `WATCHDOG_INTERVAL_DAYS` darf erst als wirksam dokumentiert werden, wenn ein späterer ausführbarer Aufrufer ihn tatsächlich einliest. GitHub-App- und Cloudflare-Konfiguration bleiben P09 vorbehalten.
