# Leser-UX und barrierearme Tabellen

Dieses Dokument beschreibt den ausgelieferten P10.3-Vertrag. Maßgeblich bleiben das
serverseitig erzeugte HTML und die lokalen Assets. JavaScript verbessert Bedienung, ist aber
weder Datenquelle noch Voraussetzung für vollständige Inhalte.

## Leser-Navigation

Die sichtbare Navigation ist absichtlich klein und in dieser Reihenfolge festgelegt:

1. Start
2. Handdesinfektion
3. Flächendesinfektion
4. Kategorien
5. Anleitungen → Übersicht
6. Sortierbare Tabelle
7. Bulletins → Übersicht
8. Methodik → Wirksamkeit, Bewertung, Sicherheit

Ein Punkt „Projekt“ oder „Wartung“ fehlt bewusst. Leserinnen und Leser sehen dort nur
fachliche Inhalte; technische Betriebsseiten werden nicht mit der fachlichen Navigation
vermischt.

Auf der Startseite führt der native Link mit Werkzeug-Symbol zur Seite „Wartung, Projekt und
Automatisierung“. Er besitzt einen zugänglichen Namen und mindestens 44 × 44 CSS-Pixel
Trefferfläche. `WARTUNG.md` und alle Seiten unter `Wartung/` bleiben durch `not_in_nav`
außerhalb der Lesernavigation, werden aber vollständig gebaut. Die Strict-Linkvalidierung
prüft Werkzeuglink, Wartungshub und alle neun Wartungsziele; ein fehlendes oder externes Ziel
bricht den Build ab.

## Landingkarten und Tastatur

Die drei Landingkarten sind native Links zu Händedesinfektion, Flächendesinfektion und
Kategorien. Damit bleiben Enter-Aktivierung, Linkkontextmenü und Browserstatus ohne eigenes
JavaScript erhalten. Auf breiten Ansichten stehen sie im Grid; bis 44 rem werden sie
einspaltig. Hover verändert Rahmen und Hintergrund. `:focus-visible` setzt einen deutlichen,
nicht nur farblich codierten Umriss für Karten, Werkzeuglink, Tabellenbuttons und
Filterfelder.

## Korpustabelle

Der Server rendert jede gültige Korpuszeile vollständig mit 14 Spalten:

1. Dokumenttyp
2. Titel
3. Jahr
4. Monat
5. RKI-Handle
6. DOI
7. Rechtezustand
8. PDF vorhanden
9. Markdownstatus
10. OCR-Status
11. Monatsarchiv
12. Jahresarchiv
13. Checksumme
14. Quelle

Standardsortierung: Jahr absteigend, Monat absteigend (`—` wie fehlend), danach Dokumenttyp,
Titel und RKI-Handle aufsteigend mit deterministischen Groß-/Kleinschreibungs-Tie-Breakern.
Fehlt `content/generated-data/corpus-table.json`, bleibt eine Tabelle mit Caption und
Spaltenköpfen sichtbar; der Text „Noch keine validierten Dokumentmanifeste“ erklärt den
zulässigen Leerzustand. Es werden keine Ersatzzeilen erfunden.

## Anleitungstabelle

Nur der vollständig freigegebene Gatezustand erzeugt die Anleitungstabelle mit 13 Spalten:

1. Rang
2. Anwendungsbereich
3. Titel
4. Wirkstoff
5. Konzentration
6. Einwirkzeit
7. Spektrum
8. Kategorien
9. Jahr
10. Zeitstatus
11. Vertrauen
12. Bulletin
13. Seite

Standardsortierung: Anwendungsbereich aufsteigend, Rang absteigend, Vertrauen in der
Reihenfolge `high`, `medium`, `low`, Jahr absteigend, danach Titel, Bulletin und Seite
aufsteigend. Rangfolgen gelten ausschließlich innerhalb des jeweiligen Anwendungskontexts;
es gibt keinen universellen Wirksamkeitspunktwert. Eine freigegebene leere Projektion ist
eine gültige Tabelle mit null Datenzeilen.

## Gate-Wahrheitstabelle

Vier Gate-Eingaben bestimmen ausschließlich die Anleitungstabelle. Die Korpusprojektion ist
davon unabhängig und darf vor dem Gate leer oder vorhanden sein.

| Status in `status.json` | `research/corpus-readiness.json` | `research/taxonomy.yml` | `content/generated-data/anleitungen.json` | Ergebnis |
| --- | --- | --- | --- | --- |
| Gate `false`, Zustand `blocked` oder `candidate` | optional; falls vorhanden ebenfalls Gate `false` | verboten | verboten | nur Korpustabelle |
| Gate `true`, Zustand `blocked` oder `candidate` | erforderlich, schema-valide und Gate `true` | verboten | verboten | nur Korpustabelle während Readiness-Review |
| Gate `true`, Zustand `proposal` | erforderlich | Proposal-Taxonomie mit passendem Manifest-Hash erforderlich | verboten | nur Korpustabelle während Taxonomie-Review |
| Gate `true`, Zustand `approved` | erforderlich und vollständig bis 2020 | freigegeben, alle Kategorien freigegeben, Evidenz passend | erforderlich, Taxonomieversion passend | Korpus- und Anleitungstabelle |

Jede andere Kombination scheitert fail-closed. Das gilt auch für Schemafehler, unbekannte
Schlüssel, doppelte Identitäten, abweichende Manifest- oder Taxonomieversionen, offene
Quell-/Konvertierungslücken und überschrittene Größen-, Zeilen- oder Strukturgrenzen.

## Sicherheit, lokale Assets und No-JS

Alle Zellen werden vor der HTML-Ausgabe escaped. Datenwerte erzeugen weder `href` noch
`src`; RKI-Quelle, DOI und Handle bleiben Text. Die Seite lädt ausschließlich das lokale
`assets/javascripts/table.js`. Das Skript liest vorhandene `<tr>` und Metadaten, verschiebt
nur bestehende Zeilen und ergänzt Sortier-/Filtercontrols. Es verwendet kein `innerHTML`,
keinen Fetch und keine zweite Datenprojektion.

Ohne JavaScript bleiben Überschriften, Captions, Status-/Leertexte und jede serverseitige
Datenzelle sichtbar. Mit JavaScript haben Filter echte Labels, der Trefferstand steht als
Text in einer höflich angekündigten Live-Region und Sortierung wird über native Buttons und
`aria-sort` vermittelt. Zustand hängt nie nur von Farbe ab.

## Mobile Nutzung und Reduced Motion

Jede Tabelle liegt in einer benannten, per Tastatur fokussierbaren Region. `max-width: 100%`
und `overflow-x: auto` halten breite Tabellen innerhalb der Seite; ein sichtbarer Hinweis
nennt horizontales Scrollen. Header bleiben kompakt, lange Zellen dürfen umbrechen. Labels,
Eingaben, Selects und Buttons besitzen ausreichende Größe. Bei
`prefers-reduced-motion: reduce` werden die nicht notwendigen UI-Transitionen und
Animationen deaktiviert.

## Abnahmegrenze

P10.3 beweist Struktur, lokale Ressourcen, serverseitige Vollständigkeit und CSS-Verträge
automatisiert. Echte Browserprüfung mit Axe, visueller Tastaturkontrolle und einer 390-Pixel-
Ansicht gehört zu P11.4. Dieses Dokument behauptet diese spätere Browserabnahme nicht.
