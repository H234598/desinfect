# RKI-Grabber: Architektur und Sicherheitsgrenzen

## Ziel von P03

Der vorhandene RKI-Grabber bleibt die fachliche Grundlage. P03 zerlegt den Monolithen ohne historischen Vollabruf in klar testbare Schichten:

```text
CLI → öffentliche API → Service/Orchestrierung
                         ├─ reine HTML-Parser
                         ├─ injizierbarer HTTP-Transport
                         └─ sicherer PDF-Downloader
```

## Module

| Modul | Verantwortung | Seiteneffekt |
|---|---|---|
| `models.py` | typisierte Request-, Quellen-, Record-, Fehler- und Resultverträge | keiner |
| `parser.py` | DSpace-Listing, Handle, Metadaten, DOI, Rechtefelder und PDF-Kandidaten | keiner |
| `http.py` | same-origin HTTPS, manuelle Redirects, Delay, Retries, Robots- und Bytegrenzen | Netzwerk |
| `download.py` | Resume, Größen-/MIME-/PDF-/Hashprüfung, atomare Ablage | nur expliziter Outputroot |
| `service.py` | Scopes, Pagination, Deduplizierung, Fehlerfortsetzung und betroffene Perioden | über Ports sichtbar |
| `api.py` | gemeinsame `grab()`-API und atomare Ergebnisdateien | nur bei explizitem materialisierendem Lauf |
| `rki_epidbull_grabber.py` | Kompatibilitäts-CLI und Exitcodes | delegiert ausschließlich |

## Trust Boundary

Externe URLs, HTML, Titel, Dateinamen, Metadaten und PDFs sind untrusted data. Der Client akzeptiert ausschließlich:

- Schema `https`,
- Host aus der festen Allowlist,
- Port 443 oder keinen expliziten Port,
- keine Benutzerinformationen,
- kein URL-Fragment,
- Redirects erst nach erneuter vollständiger Validierung.

Fremdhost-Redirects und Credential-URLs werden vor einem Folgerequest blockiert.

## Robots-Vertrag

Bei aktiviertem `respect_robots` gilt:

- HTTP 200: Regeln parsen und anwenden;
- HTTP 404: keine Datei vorhanden, Abruf bleibt erlaubt;
- andere Statuscodes, Transportfehler oder unbrauchbare Antwort: `robots.unavailable` und sicherer Block;
- explizites Disallow: `robots.denied`.

Damit wird ein unbekannter Robotszustand nicht still als Erlaubnis interpretiert.

## Downloadvertrag

Der PDF-Downloader hält Root- und Parent-Directory-Deskriptoren, folgt keinen Symlinks und schreibt in eine exklusive `.part`-Datei. Eine vorhandene Part-Datei wird nur mit einem validierten `Range`-/`Content-Range`-Vertrag fortgesetzt. Vor dem Austausch werden geprüft:

1. gemessene Bytegrenze,
2. `Content-Length`, soweit vorhanden,
3. zulässiger MIME-Typ,
4. `%PDF-` am Anfang,
5. `%%EOF` am Ende,
6. RKI-MD5, soweit vorhanden,
7. SHA-256.

Danach folgen `flush`, Datei-`fsync`, descriptor-relatives `os.replace` und Parent-Directory-`fsync`.

## Resultvertrag

`schemas/grabber-result.schema.json` ist Draft 2020-12, verbietet unbekannte Felder und enthält:

- feste Quellenbeschreibung,
- redigierte Requestprojektion,
- UTC-Start-/Endzeit,
- Outcome und Summary,
- betroffene ISO-Wochen, Monate und Jahre,
- versionierte Dokument-/Source-IDs,
- Rechte-Rohmetadaten,
- PDF-/Hash-/ETag-/Last-Modified-Felder,
- strukturierte Issues mit Code, Stage und Retrybarkeit.

Absolute Runnerpfade und Kontaktangaben werden nicht in das Ergebnis aufgenommen.

## P03-Grenzen

P03 führt nicht ein:

- `plan|materialize|apply` als vollständigen Pipelinevertrag,
- Git LFS oder andere Storageadapter,
- Rechteentscheidung für öffentliche Spiegelung,
- kanonische P06-Dokumentpfade,
- Dispatcher, GitHub-App-Commit oder Scheduler,
- echten RKI-Pilot oder historischen Backfill.

Diese Funktionen bleiben den nachfolgenden Phasen vorbehalten.
