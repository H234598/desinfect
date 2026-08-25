# Contentmodell, Wikilinks und Callouts

Dieses Dokument beschreibt den verbindlichen P10.1-Vertrag für manuell gepflegte
Markdown-Quellen unter `content/`. P10.1 stellt ausschließlich ein read-only
Contentmodell, einen Index sowie reine Konverter für Wikilinks und
Obsidian-Callouts bereit. Website-Build, MkDocs-Konfiguration, Navigation,
Tabellen und Downloadseiten gehören zu P10.2 bis P10.4.

## Quellen und Rollen

Der minimale Ausgangsbestand besteht aus:

- `index.md` mit Rolle `landing`,
- `Handdesinfektion.md` und `Flaechendesinfektion.md` mit Rolle `axis`,
- `Kategorien.md` mit Rolle `method`,
- `WARTUNG.md` mit Rolle `maintenance`.

Zulässige Rollen sind exakt `landing`, `axis`, `bulletin`, `instruction`,
`method`, `maintenance` und `generated-wrapper`. Unbekannte oder noch nicht
freigegebene Taxonomierollen werden abgewiesen. `Kategorien.md` dokumentiert bis
zum Taxonomie-Gate nur Status und Methodik; es definiert keine endgültigen
Kategorienamen oder Slugs.

Jede Seite benötigt YAML-Frontmatter mit `title` und `role`. `aliases` ist eine
optionale Liste. Weitere vorhandene RKI-Metadaten dürfen erhalten bleiben,
sofern sie den Sicherheitsvertrag erfüllen. `id` ist verboten: Identität wird
nicht von Autoren gewählt.

## Pfade, IDs und URLs

Quellpfade sind NFC-normalisierte, relative POSIX-Pfade unter `content/`. Leere,
absolute, traversierende, nicht kanonische oder nicht auf `.md` endende Pfade
werden verworfen. Symlinks und aus dem Contentroot heraus aufgelöste Pfade sind
unzulässig. Der Index blockiert zusätzlich portable Kollisionen nach Unicode-
und Case-Normalisierung.

Eine Seiten-ID ist stabil aus dem kanonischen Quellpfad abgeleitet:

```text
payload = UTF-8("desinfect-page-v1" + NUL + kanonischer POSIX-Pfad)
id = "p_" + sha256(payload).hexdigest()[:16]
```

Der Suffix besteht damit aus den ersten 16 Zeichen des kleingeschriebenen
SHA-256-Hexdigest, nicht aus 16 Digestbytes.

Der Index prüft Kollisionen von IDs, kanonischen URLs, Titeln und Aliasen.
`index.md` besitzt URL `/`; `INDEX.md` und `README.md` in Unterordnern bilden
deren Verzeichnis-URL; andere Seiten verwenden den suffixlosen Quellpfad mit
abschließendem `/`. URL- und Identitätsableitung erzeugen noch keine Navigation
oder Taxonomie.

## Sicheres Frontmatter

Frontmatter wird mit einem gehärteten PyYAML-`SafeLoader` gelesen. Folgende
Eingaben schlagen geschlossen mit `ContentModelError` fehl:

- doppelte Schlüssel auf jeder Ebene,
- YAML-Aliase, Ankergraphen und rekursive Graphen,
- mehr als 16 YAML-Ebenen oder mehr als 1024 YAML-Knoten,
- nicht-stringförmige oder nicht kanonische Metadatenschlüssel,
- nicht sichere YAML-Typen, nicht endliche Zahlen und Kontrollzeichen,
- nicht NFC-normalisierte Texte,
- unsichere Werte für `source_pdf` und Schlüssel mit Suffix `_path`,
- fehlende Pflichtfelder, unbekannte Rollen sowie doppelte Titel/Aliase.

Geladene Metadaten sind rekursiv unveränderlich. Mappings werden als read-only
Ansicht und Listen als Tupel ausgegeben. YAML-Mengen und andere nicht
unterstützte Typen werden abgewiesen. Verarbeitung ändert weder das
Quell-`str` noch Quelldateien.

## Read-only Contentindex und Überschriften

`build_content_index()` liest Markdownseiten, schreibt aber keine Dateien. Es
prüft UTF-8, Rootgrenze, Symlinks und alle oben genannten Kollisionen. Eine
Markdowndatei, deren Inhalt ein Git-LFS-Pointer ist, wird nie als Seite gelesen
oder indexiert.

Überschriften außerhalb geschützter Markdownbereiche werden mit Ebene, Text,
Zeile und Anker erfasst. Explizite `{#anker}` bleiben erhalten und müssen pro
Seite eindeutig sein. Bei wiederholten impliziten Überschriften werden
deterministisch `_1`, `_2` und folgende Suffixe vergeben.

Der gemeinsame Scanner schützt:

- YAML-Frontmatter,
- CommonMark-Fences aus Backticks oder Tilden,
- eingerückten Code,
- HTML-Kommentare,
- gültige Inline-Code-Spans, auch über mehrere Zeilen.

Ein Backtick-Fence mit Backtick im Info-String ist kein gültiger Fence;
Tilde-Fences dürfen dort Backticks enthalten. Ein nicht geschlossener oder per
ungerader Backslashzahl escapeter Backtick-Run schützt keinen nachfolgenden
Text. Nur ein gleich langer, gültiger Abschluss beendet einen Inline-Code-Span.

## Wikilinks und Auflösung

Unterstützte Formen sind `[[target]]`, `[[target|Label]]`,
`[[target#anker]]` und `[[target#anker|Label]]`. Der Scanner liefert stabile
Quelloffsets und ignoriert Links in allen geschützten Bereichen. Verschachtelte,
leere oder syntaktisch fehlerhafte Links werden nicht als gültige Wikilinks
interpretiert.

Seitenziele werden fail-closed aufgelöst über:

- pfadbasierte Seiten-ID,
- eindeutigen Titel oder Alias,
- kanonischen relativen Markdownpfad, auch aus Unterordnern,
- eindeutigen Dateinamen oder Basename,
- `INDEX`- und `README`-Varianten.

Lokale `#anker` und entfernte Seitenanker werden gegen den Index geprüft.
Mehrdeutige Ziele oder Anker, Case-Mismatches, Root-Escape, Traversal,
unaufgelöste Ziele und externe Ziele liefern einen expliziten Fehlerstatus.
Unterschiedliche Groß-/Kleinschreibung wird nicht still korrigiert.

Bild-Assets dürfen per `![[bild.png]]` eingebettet werden. PDF- und andere
zulässige lokale Assets werden als normale Links ausgegeben, nicht eingebettet.
Markdown-Transklusion und andere Asset-Embeds bleiben gesperrt. Assetpfade
dürfen den Contentroot auch über Symlinks nicht verlassen.

`convert_for_web()` arbeitet nur auf einer generierten Stringkopie.
Es ersetzt Vorkommen von hinten nach vorn, damit Quelloffsets stabil bleiben,
erzeugt relative Standard-Markdownlinks und URL-kodiert Pfade und Anker.
Automatisch erzeugte Labels werden gegen HTML und Markdown escapet. Ein nicht
auflösbares oder unsicheres Ziel bricht die Konvertierung ab.

## Obsidian-Callouts

Callouts beginnen mit `> [!type] Titel`; `+` und `-` steuern offene oder
geschlossene Faltung. Verschachtelter Blockquote-Inhalt bleibt erhalten. Die
Konvertierung erzeugt deterministische Material-Admonitions ausschließlich in
einer generierten Kopie.

Unterstützt werden die kanonischen Typen `note`, `abstract`, `info`, `tip`,
`important`, `success`, `question`, `warning`, `failure`, `danger`, `bug`,
`example`, `quote`, `evidence`, `rights`, `historical` und `safety`. Die Aliase
`summary`, `tldr`, `todo`, `hint`, `check`, `done`, `help`, `faq`, `caution`,
`attention`, `fail`, `missing`, `error` und `cite` werden auf diese Typen
abgebildet.

Unbekannte Typen und nicht wohlgeformte Callouts bleiben gemäß eingefrorenem
Referenzvertrag literal erhalten. Titel sind auf 200 Zeichen begrenzt,
NFC-normalisiert, kontrollzeichenfrei und gegen HTML, Markdown, Quotes und
Backslashes escapet. Calloutsyntax in Frontmatter, Code-Fences, eingerücktem
Code, HTML-Kommentaren und gültigem Inline-Code bleibt byteidentisch.

## Reinheit und Verifikation

Indexierung und Konverter besitzen keine Dateischreiboperation. Wiederholte
Konvertierung derselben Quelle mit demselben Index ist bytegleich. Tests prüfen
zusätzlich die SHA-256-Hashes aller fünf Contentquellen vor und nach reinem
In-Memory-Rendering.

Fokussierte Prüfung:

```bash
pytest -q tests/web/test_content_model.py tests/web/test_content_index.py tests/web/test_links.py tests/web/test_callouts.py
ruff check scripts/web tests/web
ruff format --check scripts/web tests/web
```
