# Offline-Testfixtures

Alle normalen Tests laufen ohne Netzwerk und ohne Git-LFS-Download. Die kleinen
Payloads unter diesem Verzeichnis sind synthetisch oder stark minimiert. Jede
Fixture ist in `manifest.json` mit Pfad, Größe, SHA-256, Medientyp, Zweck,
Provenienz und Lizenzstatus registriert.

Regeln:

- maximal 64 KiB je Fixture;
- keine Secrets, Symlinks, unregistrierten Dateien oder massenhaften RKI-Volltexte;
- echte Netzwerkprüfungen benötigen den Marker `network` und
  `DESINFECT_ALLOW_NETWORK_TESTS=1`;
- Änderungen an einer Fixture erfordern eine bewusste Manifeständerung im selben
  Pull Request.
