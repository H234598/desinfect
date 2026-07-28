
# Öffentlicher Status, Laufstatus und Recovery

`status.json` ist die kleine öffentliche Betriebsakte. Vollständige Lauf-, Fehler-, Artefakt- und Recoverydaten folgen `schemas/run-manifest.schema.json` und werden nicht ungefiltert in den öffentlichen Snapshot kopiert.

Drei Zeitwerte bleiben getrennt:

1. beobachteter letzter Commit auf `main`;
2. letzter erfolgreich abgeschlossener Lauf;
3. letzter erfolgreicher Lauf mit beabsichtigter persistenter Änderung.

Ein fehlgeschlagener oder blockierter Lauf erhöht lediglich den Fehlerzähler und setzt einen redigierten Fehlerhinweis. Er erneuert keinen Erfolgszeitpunkt. Änderungen verwenden eine optimistische `revision`; ein veralteter Writer wird abgewiesen.

Die CLI `python3 -m scripts.rki_pipeline.runtime_status_cli` unterstützt Start, Update, Abschluss und validierte Wiederherstellung. Tokenfamilien, Bearer-Werte, Passwortzuweisungen, E-Mail-Adressen sowie URL-Querystrings und Fragmente werden vor Persistenz redigiert.
