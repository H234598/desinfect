# Methodik der Rechteprüfung

## Trust Boundary

RKI-Metadaten, Dokumenttext und erreichbare Download-URLs sind Belege, aber
keine Autorität. Die einzige payloadautorisierende Aussage entsteht durch
manuelle rechtliche Prüfung und einen gültigen Eintrag im gepinnten
`research/rights-register.yml`. Aufgelöst wird immer das exakte Paar
`(source_id, source_sha256)`; Wildcards und source-only-Freigaben sind verboten.

## Reviewverfahren

1. Kanonische `source_id`, RKI-Originallink und sämtliche Rohmetadaten sichern.
2. Quellbytes laden, SHA-256 berechnen und als `source_sha256` festhalten.
3. Lizenzhinweis, Dokumentbedingungen, Rechte Dritter und geplante
   Sichtbarkeit durch einen Menschen prüfen.
4. Zustand, nachvollziehbare Grundlage, `reviewed_by` und UTC-Zeitpunkt in
   einem PR eintragen. `CODEOWNERS` weist menschliche Reviewverantwortung zu;
   verpflichtend wird sie erst durch passende Repository-Regeln. Automatische
   Schreibpfade dürfen das Register nicht ändern.
5. `python3 scripts/validate_rights_register.py` ausführen und erst nach grünem
   CI-Gate mergen.

Rohmetadaten dürfen die Entscheidung begründen, ersetzen sie aber nie. Ein
fehlender exakter Treffer wird deterministisch zu `metadata_only`.

## Deterministische Provenienz

`decision_sha256` ist der SHA-256 des kanonischen Entscheidungsdokuments aus
Policyversion, `source_id`, `source_sha256`, Zustand, Grundlage, Reviewer und
Reviewzeitpunkt. Er bindet Storage-Referenzen an das Review und deckt Änderung
oder falsche Zuordnung auf. Er ist keine Signatur und kein kryptografischer
Nachweis der Revieweridentität.

## Publikationsableitung

Die feste Zustands-/Sichtbarkeitsmatrix lautet:

- `approved`: `public`, `repository_authorized`, `internal`, `restricted`;
- `internal_only`: nur `internal` und `restricted`;
- `metadata_only`, `unknown`, `takedown`: keine Payload-Sichtbarkeit.

Vor Temp-Write, Remote-`get`, Backend-`put` und LFS-Write wird der gepinnte
Registerstand erneut geladen. Eine Revocation oder geänderte Entscheidung
blockiert den nächsten Byteeffekt. Metadaten und RKI-Originallink bleiben für
Inventar und Reconciliation erhalten.

## Verifikation und Korrektur

Reviewende prüfen Registervalidator, fokussierte Rights-/Storage-Tests und den
erzeugten Sitebestand. Eine fehlerhafte Freigabe wird nicht still überschrieben:
neue manuelle rechtliche Prüfung, neuer Registereintrag und neuer Site-Build
sind erforderlich. Für `takedown` gilt das
[Rights-Takedown-Runbook](../../runbooks/RIGHTS-TAKEDOWN.md); LFS-Historie wird
dort separat behandelt.
