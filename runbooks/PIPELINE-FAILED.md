# Pipeline Failed

1. GitHub-Job Summary öffnen. Workflowstatus und Transaktionsstatus getrennt lesen; ein grünes Transaktionsmanifest widerlegt keinen späteren Gate-, Commit- oder Pushfehler.
2. Zeitlich begrenztes Artefakt `rki-transaction-<run-id>` sichern. Dispatchplan, Transaktionsergebnis, Commitplan und Summary nur als Diagnose behandeln.
3. RunManifest und `status.json` lokal schema-validieren. Keine beschädigte Statusdatei manuell auf `operational` setzen und keine erfolgreichen Uhren fortschreiben.
4. Fehlercode, Phase, `consecutive_failures`, letzte erfolgreiche Lauf-/Schreibzeit und empfohlene Recovery-Aktion prüfen. Redigierte Werte nicht durch Rohlogs mit Tokens oder personenbezogenen Daten ersetzen.
5. Bei offenem Marker-Issue `<!-- desinfect:rki-pipeline-incident:v1 -->` nur dieses Issue verwenden. Kein zweites Betriebsissue anlegen. Bei mehreren Marker-Issues Automatik deaktivieren und Duplikate manuell klären.
6. Fehlerursache beheben und denselben kanonischen Dispatchplan nur dann erneut verwenden, wenn dessen Base-SHA noch `main` entspricht. Sonst neu planen. Nie force-pushen.
7. Nach erfolgreichem Lauf prüfen: Job Summary grün, Erfolgsuhren plausibel, Fehlerzähler null, Rolling Issue kommentiert und geschlossen. Diagnoseartefakte gemäß Retention auslaufen lassen; nicht ins Repository kopieren.

## Rolling Issue deaktivieren

Repositoryvariable `ROLLING_ISSUE_ENABLED` entfernen oder ungleich `true` setzen. Dadurch entfallen Issue-Token und Issuezugriff; Summary sowie Diagnoseartefakte bleiben aktiv. Benötigt die App danach keine anderen Issueoperationen, `Issues: write` in der App-Konfiguration wieder entfernen.

