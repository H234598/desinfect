---
title: "Rechte und Publikationsmodi"
role: "method"
aliases:
  - "Rechte"
  - "Publikationsmodi"
---

# Rechte und Publikationsmodi

Diese Übersicht erklärt den technischen Prüfvertrag des Projekts. Sie ist **keine
Rechtsberatung**, erteilt keine automatische Serienfreigabe und ersetzt keine Prüfung des
Einzelfalls.

## Exakte Entscheidung statt Pauschalfreigabe

Eine Rechteentscheidung gilt nur für den Vierfachschlüssel
(`source_id`, `canonical_url`, `version_or_bitstream`, `source_sha256`). Änderung eines
Feldes setzt die technische Freigabe zurück. Es gibt keine Freigabe nach Host, Serie, Jahr
oder URL-Muster. Fehlt der exakte Treffer, gilt `unknown` mit `origin_link`.

Die geschlossenen Publikationsmodi sind:

- `remove_all`: keine öffentliche Präsenz;
- `origin_link`: nur der kanonische RKI-Originallink;
- `source_only`: sichtbare Quellenmetadaten ohne Payload;
- `materialized`: nur einzeln geprüfte öffentliche Artefakte.

Jede technische Wirkung wird separat geprüft: `fetch`, `cache`, `hash`, `ocr`,
`extract_text`, `thumbnail`, `index_text` und `publish`. Nur `materialized` kann solche
Aktionen tragen. Ein Backend, vorhandene Dateien oder öffentliche Erreichbarkeit erweitern
die Entscheidung nicht.

## Manuelle Rechts- und Komponentenprüfung

Mögliche Grundlagen werden ausschließlich manuell am konkreten Dokument geprüft. Das gilt
auch für [§ 5 UrhG](https://www.gesetze-im-internet.de/urhg/__5.html) und
[§ 12a EGovG](https://www.gesetze-im-internet.de/egovg/__12a.html). Keiner dieser Verweise
bewirkt automatisch eine Freigabe.

Bei einem dokumentierten Hinweis auf CC BY 4.0 werden die
[verständliche Lizenzfassung](https://creativecommons.org/licenses/by/4.0/deed.de) und der
[deutsche Rechtstext](https://creativecommons.org/licenses/by/4.0/legalcode.de) geprüft.
`publish` erfordert vollständige Attribution sowie eine gesonderte Komponentenprüfung für
Rechte Dritter. Attribution umfasst Urheber, Attributionsparteien, Copyright-, Lizenz- und
Disclaimerhinweis, kanonische Lizenz- und Origin-URL sowie frühere und aktuelle
Änderungshinweise.

Die [RKI-Originallink-Regel](https://edoc.rki.de/) erlaubt ausschließlich den zum
`source_id` passenden kanonischen Handle-Link. Sie erlaubt weder Payloadzugriff noch eine
abweichende, umgeleitete oder mit Queryparametern versehene URL.

## Downgrade, Takedown und spätere Einordnung

Ein Downgrade entfernt alte Downloadreferenzen beim nächsten Build. `source_only` behält nur
zulässige Quellenmetadaten; `origin_link` nur den kanonischen RKI-Link. Ein Takedown mit
`remove_all` entfernt Titel, Bibliografie, URL und Artefaktspuren aus der öffentlichen
Projektion. Wiederveröffentlichung benötigt eine neue manuelle Entscheidung; sie wird nicht
aus einem alten Hash oder früheren Sitebestand abgeleitet.

Die spätere fachliche, medizinische und rechtliche Einordnung gehört zu P16. Diese Seite
nimmt sie nicht vor und leitet aus historischen Quellen keine heutige Empfehlung ab.
