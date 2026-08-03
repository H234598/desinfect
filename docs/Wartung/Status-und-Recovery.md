
# Öffentlicher Status, Laufstatus und Recovery

`status.json` ist die kleine öffentliche Betriebsakte. Vollständige Lauf-, Fehler-, Artefakt- und Recoverydaten folgen `schemas/run-manifest.schema.json` und werden nicht ungefiltert in den öffentlichen Snapshot kopiert.

Drei Zeitwerte bleiben getrennt:

1. beobachteter letzter Commit auf `main`;
2. letzter erfolgreich abgeschlossener Lauf;
3. letzter erfolgreicher Lauf mit beabsichtigter persistenter Änderung.

Ein fehlgeschlagener oder blockierter Lauf erhöht lediglich den Fehlerzähler und setzt einen redigierten Fehlerhinweis. Er erneuert keinen Erfolgszeitpunkt. Änderungen verwenden eine optimistische `revision`; ein veralteter Writer wird abgewiesen.

Die CLI `python3 -m scripts.rki_pipeline.runtime_status_cli` unterstützt Start, Update, Abschluss und validierte Wiederherstellung. Tokenfamilien, Bearer-Werte, Passwortzuweisungen, E-Mail-Adressen sowie URL-Querystrings und Fragmente werden vor Persistenz redigiert.

## Täglicher Dispatcher und Catch-up

`.github/workflows/rki-dispatcher.yml` läuft täglich um `04:17 UTC` (`17 4 * * *`) und kann zusätzlich manuell gestartet werden. Er liest `status.json`, berechnet nur abgeschlossene Zeiträume und schreibt bei der Planung nicht in das Repository. Catch-up beginnt direkt nach dem jeweiligen Wasserstand und ist pro Lauf durch `config/dispatcher.toml` begrenzt:

- höchstens 8 Wochen, 3 Monate und 1 Jahr;
- höchstens 1 Reconciliation bei einem Intervall von 92 Tagen.

Ein fehlender Wasserstand löst keinen historischen Vollabruf aus, sondern höchstens die zuletzt abgeschlossene Periode. Gibt es keine fällige Aufgabe, endet der Dispatcher ohne Pipeline. Alle fälligen Aufgaben laufen sonst gemeinsam durch eine Transaktion und genau eine globale Validierung. Erst eine verifizierte Änderung darf höchstens einen Commit erzeugen; No-op-Läufe erzeugen keinen Commit.

Die gemeinsame Concurrency-Gruppe `desinfect-repository-writer` reiht weitere Writer ein und bricht einen laufenden Writer nicht ab. Ändert sich `main` nach der Dispatchplanung, verweigert der Writer den Push ohne Force-Option. Recovery besteht aus einem vollständigen neuen Lauf gegen den aktuellen `main`-Stand; ein alter Dispatch- oder Commitplan wird nicht wiederverwendet.

## Manueller Backfill

`.github/workflows/rki-backfill.yml` besitzt keinen Zeitplan. Operatoren geben `from_year`, `to_year`, `max_tasks` und den RunMode explizit an; `config/dispatcher.toml` begrenzt zusätzlich auf höchstens 1000 Aufgaben und Jahre ab 1994. `plan` und `materialize` bleiben ohne Repositoryschreibzugriff. `apply` wird nur ausgeführt, wenn `confirm_apply` exakt `APPLY` lautet.

Backfill verwendet dieselbe Transaktion, Validierung, No-op-Behandlung, Concurrency und Base-Drift-Prüfung wie der tägliche Lauf. Bei Base-Drift wird der Backfill mit denselben geprüften Bereichsgrenzen manuell neu gestartet.
