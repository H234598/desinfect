# Sichere Datei- und Stagingoperationen

`scripts/rki_pipeline/io_utils.py` und `staging.py` bilden die einzige erlaubte
Basis für spätere persistente Dateioperationen.

## Garantien

- relative NFC-/POSIX-Pfade ohne Traversal oder Backslashes;
- Root-Grenze und vorhandene Symlinkkomponenten werden geprüft;
- portable Casefold-/Unicode-Kollisionen werden vor dem Schreiben erkannt;
- SHA-256 wird gestreamt;
- JSON wird UTF-8-freundlich, sortiert und newline-terminiert serialisiert;
- atomare Dateiablage über `.part`, `flush`, Datei-`fsync`, `os.replace` und
  Verzeichnis-`fsync`;
- generierte Verzeichnisse benötigen `.desinfect-generated-root`;
- Staging und Ziel müssen auf demselben Dateisystem liegen;
- eine nicht blockierende, parent-weite `flock` serialisiert alle Zielnamen desselben
  Parent-Verzeichnisses; parallele Geschwisterziele schlagen sofort geschlossen fehl;
- nach Staging-zu-Ziel-Umbenennung bleibt exakt derselbe Verzeichnis-FD bis
  Transaktionsende offen; Validator, Ownership-Prüfung und Rollback verwenden diese
  gepinnte Inode-Identität;
- Veröffentlichung blockiert Abbruchsignale von Rename über Parent-`fsync` und
  Commit-Markierung; erst danach werden Signale wieder freigegeben;
- bei Fehlern bleibt der vorherige vollständige Zielstand erhalten.

Unmarkierte Verzeichnisse werden niemals automatisch ersetzt oder gelöscht.
