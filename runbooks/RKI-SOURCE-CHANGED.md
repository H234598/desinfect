# RKI Source Changed

Bei einem P07.3-Finding gilt diese sichere Reihenfolge:

1. Reconciliationreport, kanonische `Quellen/manifest.jsonl` und verwendeten
   Remotemetadatensnapshot unverändert sichern. Zeitpunkt, Bereich und
   `source_manifest_sha256` notieren.
2. Finding-Code und Subject-ID aus der Plan-Evidenz bestimmen. Keine Quelle
   wegen eines Findings blind herunterladen.
3. Bei `changed` nur die begrenzte Kandidatmaterialisierung für bereits durch
   driftende Metadaten begründete Quelle erneut ausführen und Hash/Größe gegen
   die Evidenz prüfen.
4. Bei `rights_changed` jede Publikation stoppen. Kanonisches
   `research/rights-register.yml` und die aktuelle Rechteauthority reviewen;
   keine Entscheidung aus Remote-Metadaten ableiten.
5. Bei Storage- oder Archivdrift passenden P04-Adapter und Objekt prüfen.
   Produkte über P05/P07 regenerieren; ZIP, Sidecar oder Periodenmanifest
   niemals manuell editieren.
6. Danach zuerst `plan`, dann `materialize` erneut ausführen:

   ```bash
   python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
   python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
   ```

7. Wasserstand nur nach Ergebnis `success` mit nicht-null `successful_at`
   fortschreiben. `blocked` aktualisiert ihn nie.
8. Unaufgelöste Remote-Löschung oder `orphan` eskalieren. Nicht automatisch
   löschen, keine History umschreiben und keine Backendbereinigung ohne
   dokumentierte Freigabe, Retention-Prüfung und Recovery-Plan vornehmen.

Reconciliation ist Diagnose, keine Reparatur- oder Veröffentlichungsoperation.
