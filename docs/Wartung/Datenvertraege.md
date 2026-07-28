
# Datenverträge und Versionsstrategie

P02 führt zwölf strikte JSON-Schema-Verträge auf Basis von Draft 2020-12 ein. Jeder Vertrag besitzt eine semantische `schema_version`, `additionalProperties: false`, portable IDs und deterministische Serialisierung.

`config/schema-registry.json` ist die kanonische Übersicht. Leser unterstützen höchstens die aktuelle und genau eine registrierte Vorversion. Der einzige P02-Migrationspfad führt den öffentlichen Status von `2.0.0` auf `3.0.0`; unbekannte oder ältere Versionen brechen fail-closed ab.

Schema-, Registry- und Migrationsänderungen sind Infrastrukturänderungen. Sie werden nicht durch einen automatischen RKI-Datenlauf geschrieben.

Die Invarianten **ADR-003 = A** und **ADR-014 = B** bleiben Bestandteil der Verträge. Insbesondere sind Analysevollständigkeit und öffentliche Spiegelvollständigkeit getrennte Felder.
