# Changelog

## 0.2.0 - 2026-07-11

- Add offline-friendly Windows control-PC setup and launcher scripts.
- Add staged simulator, filesystem, Hello, and first-measurement acceptance scripts.
- Add cross-process command locking and per-command JSON audit records.
- Add persistent recovery markers that block retries after ambiguous timeouts.
- Add `doctor` readiness checks and measurement run manifests.
- Make Spectrum execution plan-only unless `--execute` is supplied.
- Default first measurements to current-cell mode and `Discharge=OFF`.
- Correlate automatic exports with SampleID and record SHA-256 metadata.

## 0.1.0 - 2026-07-11

- Initial LabSolutions text-exchange client, simulator, CLI, and unit tests.
