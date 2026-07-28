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
- bei Fehlern bleibt der vorherige vollständige Zielstand erhalten.

Unmarkierte Verzeichnisse werden niemals automatisch ersetzt oder gelöscht.
