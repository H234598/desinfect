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
6. Produktivprüfung über die P05-Orchestrierung vorbereiten: kanonischen
   Manifestkatalog, auf `from_year` bis `to_year` begrenzten
   Remotemetadatensnapshot, konfigurierte P04-Storageadapter, `period_root`,
   `load_rights_authority()` und `load_rights_policy()` aus ihren kanonischen
   Quellen laden.
7. `plan_reconciliation(...)` mit explizitem UTC-`as_of` ausführen. Bei
   `blocked` Findings prüfen; weder Report noch Bestand verändern. Nur ein
   `success`-Ergebnis darf über `materialize_reconciliation(...)` in den
   transaktionalen P05-Temporärroot übernommen werden.
8. Wasserstand nur nach Ergebnis `success` mit nicht-null `successful_at`
   fortschreiben. `blocked` aktualisiert ihn nie.
9. Unaufgelöste Remote-Löschung oder `orphan` eskalieren. Nicht automatisch
   löschen, keine History umschreiben und keine Backendbereinigung ohne
   dokumentierte Freigabe, Retention-Prüfung und Recovery-Plan vornehmen.

## Offline-Drill

Folgende Fixture-Befehle prüfen nur den isolierten, synthetischen CLI-Vertrag.
Sie inspizieren keinen Produktivbestand und ersetzen die Schritte 6–7 nicht:

```bash
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
```

Reconciliation ist Diagnose, keine Reparatur- oder Veröffentlichungsoperation.
