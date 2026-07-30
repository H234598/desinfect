# Sicherheitsrichtlinie

## P01-Sicherheitsgrenzen

- Modulimporte führen weder Netzwerkzugriffe noch Dateischreibvorgänge oder Prozessabbrüche aus.
- Kanonische Pfade werden NFC-normalisiert, als relative POSIX-Pfade geprüft und gegen Traversal, Backslashes, Symlinks sowie portable Casefold-Kollisionen geschützt.
- Dateien werden über exklusive `.part`-Dateien, `flush`, `fsync` und `os.replace` atomar ersetzt.
- Generierte Verzeichnisse dürfen nur mit der Sentineldatei `.desinfect-generated-root` ersetzt oder gelöscht werden.
- Unit- und Fixturetests laufen ohne Netzwerk. Fixtures sind klein, SHA-256-manifestiert und secretgescannt.

## P02-Vertrags- und Schreibgrenzen

- Dauerhafte JSON-Artefakte verwenden Draft 2020-12, feste Versionsverträge und `additionalProperties: false`.
- Laufänderungen verwenden optimistische Revisionen und einen expliziten Zustandsautomaten.
- Fehler- und Recoverytexte werden vor Persistenz auf Tokens, Bearer-Werte, Passwortzuweisungen, E-Mail-Adressen und signierte URLbestandteile geprüft.
- `status.json` trennt beobachteten `main`-Commit, letzten erfolgreichen Lauf und letzten erfolgreichen Schreiblauf.
- Analysevollständigkeit und öffentliche Spiegelvollständigkeit bleiben entsprechend **ADR-014=B** getrennt.
- Automatische Writes sind deny-first begrenzt. Unbekannte Pfade, Infrastruktur, Schemas, Symlinks, Gitlinks und portable Kollisionen blockieren vor dem Commit.

## P03-RKI-Trust-Boundary

- Der Grabber akzeptiert ausschließlich `https://edoc.rki.de`, Port 443 und URLs ohne Credentials; TOML und API können diese Netzwerkgrenze nicht erweitern.
- Jeder Redirect wird manuell und erneut gegen dieselbe feste Trust-Boundary geprüft.
- Bei aktiviertem Robots-Schutz sind Fehler und unbekannte Antworten fail-closed; nur eine fehlende `robots.txt` mit HTTP 404 gilt als keine Regeldatei.
- HTML-, Robots- und PDF-Antworten besitzen harte Bytegrenzen; deklarierte und tatsächlich gelesene Größen werden geprüft.
- PDFs benötigen erlaubten MIME-Typ, `%PDF-`, `%%EOF`, eine passende optionale RKI-MD5-Prüfsumme und eine berechnete SHA-256.
- Downloads verwenden gehaltene Root-/Parent-Deskriptoren und `O_NOFOLLOW`. Ein persistenter, descriptor-relativ geöffneter `.lock` wird mit `flock(LOCK_EX|LOCK_NB)` über die gesamte Existing-/Resume-/Validierungs-/`fsync`-/Replace-Sequenz gehalten; ein zweiter Writer bricht vor jedem Netzwerkabruf retrybar ab.
- Externe Titel und Dateinamen werden nur als Daten behandelt; dauerhafte Pfade enthalten die versionierte Handle-Identität.
- Das Resultat enthält keine Kontaktangabe, keine absoluten Runnerpfade und keine ungefilterten Transportobjekte.

Sicherheitsrelevante Funde sollen nicht in öffentlichen Issues mit Secrets oder ungekürzten sensitiven Daten veröffentlicht werden. Der konkrete private Meldeweg wird in P11.3 zusammen mit der vollständigen Supply-Chain-Policy festgelegt.
