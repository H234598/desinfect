# Methodik der Rechteprüfung

## Trust Boundary

RKI-Metadaten, Dokumenttext und erreichbare Download-URLs sind Belege, aber
keine Autorität. Die einzige payloadautorisierende Aussage entsteht durch
manuelle rechtliche Prüfung und einen gültigen Eintrag im gepinnten
`research/rights-register.yml`. Aufgelöst wird immer der exakte Schlüssel
`(source_id, canonical_url, version_or_bitstream, source_sha256)`.
`canonical_url` ist die kanonische `source-manifest.bitstream_url`,
`version_or_bitstream` die kanonische `bitstream_id`; `source_url` bleibt der
Originallink. Wildcards und source-only-Freigaben sind verboten.

## Reviewverfahren

1. Kanonische `source_id`, RKI-Originallink und sämtliche Rohmetadaten sichern.
2. Kanonische Bitstream-URL und Bitstream-ID ableiten, Quellbytes laden,
   SHA-256 berechnen und den vollständigen Vierfachschlüssel festhalten.
3. Jede benötigte Aktion, Lizenzhinweis, Dokumentbedingungen, Komponenten,
   Rechte Dritter und vollständige Attribution durch einen Menschen prüfen.
4. Zustand, Modus, sortierte Aktionen, Komponentenstatus, Attribution,
   nachvollziehbare Grundlage, `reviewed_by` und UTC-Zeitpunkt in
   einem PR eintragen. `CODEOWNERS` weist menschliche Reviewverantwortung zu;
   verpflichtend wird sie erst durch passende Repository-Regeln. Automatische
   Schreibpfade dürfen das Register nicht ändern.
5. `python3 scripts/validate_rights_register.py` ausführen und erst nach grünem
   CI-Gate mergen.

Rohmetadaten dürfen die Entscheidung begründen, ersetzen sie aber nie. Ein
fehlender exakter Treffer wird deterministisch zu `unknown`/`origin_link`
ohne Aktion, Attribution oder Entscheidungs-SHA.

## Deterministische Provenienz

`decision_sha256` ist der SHA-256 des kanonischen Entscheidungsdokuments aus
Policyversion, Vierfachschlüssel, Zustand, Modus, Aktionen,
`components_state`, Attribution, Grundlage, Reviewer und Reviewzeitpunkt. Er
bindet Storage-Referenzen an das Review und deckt Änderung oder falsche
Zuordnung auf. Er ist keine Signatur und kein kryptografischer Nachweis der
Revieweridentität.

## Publikationsableitung und Effektgrenze

Die feste Modusfolge lautet `remove_all`, `origin_link`, `source_only`,
`materialized`. Nur `materialized` darf Aktionen tragen und erfordert
`approved`. Jede Wirkung wird separat als `fetch`, `cache`, `hash`, `ocr`,
`extract_text`, `thumbnail`, `index_text` oder `publish` geprüft. `publish`
verlangt freigegebene Komponenten und vollständige Attribution.

Storage ordnet reale Effekte total zu: Materialisierung/Archiv/Outputs zu
`cache`, Export zu `fetch`, Verifikation zu `hash`, Konvertierung zu
`extract_text` beziehungsweise `ocr`. Öffentlicher Remote-Apply benötigt
`cache` und `publish`; LFS- und nichtöffentlicher Apply nur `cache`.

Vor Temp-Write, Remote-`get`, Backend-`put` und LFS-Write wird der gepinnte
Registerstand erneut geladen. Eine Revocation oder geänderte Entscheidung
blockiert den nächsten Byteeffekt. `exists`, Conversion-Skips und
Manifestkatalogprüfung bleiben reine Lookup-/Validierungspfade. Takedown nutzt
`remove_all`; ein fehlender oder unbekannter Treffer bleibt `origin_link`.

## Verifikation und Korrektur

Reviewende prüfen Registervalidator, fokussierte Rights-/Storage-Tests und den
erzeugten Sitebestand. Eine fehlerhafte Freigabe wird nicht still überschrieben:
neue manuelle rechtliche Prüfung, neuer Registereintrag und neuer Site-Build
sind erforderlich. Für `takedown` gilt das
[Rights-Takedown-Runbook](../../runbooks/RIGHTS-TAKEDOWN.md); LFS-Historie wird
dort separat behandelt.
