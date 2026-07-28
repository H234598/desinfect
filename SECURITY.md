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

Sicherheitsrelevante Funde sollen nicht in öffentlichen Issues mit Secrets oder ungekürzten sensitiven Daten veröffentlicht werden. Der konkrete private Meldeweg wird in P11.3 zusammen mit der vollständigen Supply-Chain-Policy festgelegt.
