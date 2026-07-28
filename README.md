# desinfect

`desinfect` wird zu einer reproduzierbaren Archiv-, Konvertierungs-, Überwachungs- und Publikationspipeline für RKI-Epidemiologische Bulletins ausgebaut.

## Umsetzung

- [Revisionsbaseline](docs/REVISIONSBASELINE.md)
- [Implementierungsstatus](docs/IMPLEMENTIERUNGSSTATUS.md)
- [Steuerungsplan](docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md)
- [Architecture Decision Records](docs/adr/README.md)
- [Anforderungsregister](docs/requirements/README.md)
- [Vorhandener RKI-Grabber](scripts/rki_grabber/RKI_EpidBull_Grabber_README.md)

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind verbindliche, automatisiert geprüfte Architekturentscheidungen.

## P00-Baseline prüfen

```bash
python scripts/validate_all_baseline.py
python -m unittest discover -s tests -p "test_*.py"
```

Ein Arbeitspaket wird erst nach Merge, grüner CI und vollständiger Evidenz als `umgesetzt` geführt.
