# RKI-Bulletins

Kanonische Artefaktwurzel: `rki/Bulletins/`.

- Gesamtausgaben: `Jahre/<YYYY>/PDF/` und `Jahre/<YYYY>/Markdown/`.
- Einzelartikel: `Einzelartikel/<YYYY>/<MM>/PDF/` und `Einzelartikel/<YYYY>/<MM>/Markdown/`.
- Manifeste speichern vollständige `document_id` und `bitstream_id`. Dateinamen dürfen bei Portabilitätsgrenzen auf SHA-256-Tokens verkürzt werden; Identität bleibt im Manifest vollständig.
- PDFs und materialisiertes Markdown sind Git-LFS-Artefakte. JSON-Manifeste bleiben normale Git-Dateien.
- Rohes Rechte-Metadatum ist kein Recht zur Veröffentlichung. Ohne dokumentierte Entscheidung bleibt Rechtezustand `unknown` mit `rights_policy_pending`.
