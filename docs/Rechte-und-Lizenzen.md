# Rechte und Lizenzen

## Autoritative Entscheidung

Volltexte, PDFs und ZIP-Dateien dürfen nur mit einer aktuellen, manuell
reviewten Entscheidung aus `research/rights-register.yml` materialisiert,
exportiert oder veröffentlicht werden. Autorität besitzt ausschließlich der
exakte Verbundschlüssel aus `source_id` und `source_sha256`. Eine Freigabe nur
für eine Handle-ID, Serie, URL oder ein Dateimuster ist unzulässig.

RKI-Rohmetadaten wie Lizenz-URL, Rechtehinweis und Open-Access-Angabe bleiben
prüfbare Evidenz, sind aber keine Autorität für eine Veröffentlichung. Fehlt
der exakte Registereintrag, gilt `metadata_only`.

## Zustände und Sichtbarkeit

| Rechtezustand | Erlaubte Volltext-Sichtbarkeit | Wirkung |
|---|---|---|
| `approved` | `public`, `repository_authorized`, `internal`, `restricted` | Payload gemäß gewählter Sichtbarkeit zulässig |
| `internal_only` | `internal`, `restricted` | keine öffentliche oder Repository-Veröffentlichung |
| `metadata_only` | keine | nur Metadaten und RKI-Originallink |
| `unknown` | keine | keine Payload-Operation |
| `takedown` | keine | Referenzen deaktivieren; Takedown-Ablauf starten |

Die Matrix ist in `config/rights-policy.toml` festgeschrieben. Storage-Backend,
Dateityp oder bekannte öffentliche Erreichbarkeit erweitern sie nicht.

## Review und Entscheidungsprovenienz

Jede Freigabe oder Einschränkung benötigt eine manuelle rechtliche Prüfung.
Reviewer dokumentieren Grundlage, `reviewed_by`, UTC-Zeitpunkt und die exakt
geprüften Quellbytes. Abweichende Dokumentbedingungen und Rechte Dritter sind
gesondert zu berücksichtigen.

`decision_sha256` bindet Policyversion, Quellidentität, Zustand, Grundlage und
Reviewdaten deterministisch aneinander. Der Hash macht Drift sichtbar; er ist
keine Signatur, keine Identitätsbestätigung und kein Ersatz für das Review.

## Governance und Laufzeitgrenze

Der kanonische Registerpfad ist in der `RightsAuthority` gepinnt. Jeder
payloadfähige Storage-Schritt lädt ihn vor dem nächsten Byteeffekt erneut.
Dadurch blockiert eine zwischen Materialisierung und Apply eingetragene
Revocation sofort; ein alter Zustand oder `decision_sha256` wird abgewiesen.

`CODEOWNERS` weist das Register einem menschlichen Owner zu. Die Policy für
automatische Schreibpfade verweigert Änderungen an
`research/rights-register.yml`; Änderungen erfolgen nur als reviewter PR.
Beide blockierenden Workflows führen aus:

```bash
python3 scripts/validate_rights_register.py
```

Methodische Details stehen unter [Methodik/Rechte](Methodik/Rechte.md). Bei
einer Rücknahme gilt das [Rights-Takedown-Runbook](../runbooks/RIGHTS-TAKEDOWN.md).
