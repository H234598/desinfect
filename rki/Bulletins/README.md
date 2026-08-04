# RKI-Bulletins

Kanonische Artefaktwurzel: `rki/Bulletins/`.

- Gesamtausgaben: `Jahre/<YYYY>/PDF/` und `Jahre/<YYYY>/Markdown/`.
- Einzelartikel: `Einzelartikel/<YYYY>/<MM>/PDF/` und `Einzelartikel/<YYYY>/<MM>/Markdown/`.
- Generierte Periodenarchive: Wochen unter `Monate/<Montag-YYYY>/<Montag-MM>/ZIP/Wochen/`, Monate unter `Monate/YYYY/MM/ZIP/`, Monatsindizes unter `Monate/YYYY/MM/Markdown/index.md`, Jahre unter `Jahre/YYYY/ZIP/`; Periodenmanifeste unter `Manifeste/Archive/<week|month|year>/`.
- Perioden-ZIPs enthalten direkte PDF- oder Markdown-Payloads, keine leeren oder verschachtelten ZIPs.
- Manifeste speichern vollständige `document_id` und `bitstream_id`. Dateinamen dürfen bei Portabilitätsgrenzen auf SHA-256-Tokens verkürzt werden; Identität bleibt im Manifest vollständig.
- PDFs und materialisiertes Markdown sind Git-LFS-Artefakte. JSON-Manifeste bleiben normale Git-Dateien.
- Rohes Rechte-Metadatum ist kein Recht zur Veröffentlichung. Ohne dokumentierte Entscheidung bleibt Rechtezustand `unknown` mit `rights_policy_pending`.
