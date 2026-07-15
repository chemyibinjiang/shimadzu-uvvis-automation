# Changelog

## Unreleased

- Add the read-only `plan_uvvis_measurement` MCP tool for Spectrum, Photometric,
  Quantitation, and Time Course requests.
- Add strict mode-specific parameter validation, method-template selection, generated
  method naming, readiness checks, and official command-sequence plans.
- Register all four LabSolutions method/data extensions, including the local `.vtmm`
  and manual `.vtcm` Time Course method variants.
- Keep source templates immutable and block execution readiness until a generated
  method file exists.

## 0.5.0 - 2026-07-14

- Add the read-only `plan_uvvis_scan` stdio MCP tool for AI tutor integration.
- Return structured profile resolution, point count, timing, readiness, and command planning data.
- Mark the MCP tool as read-only, idempotent, and non-destructive.
- Keep the MCP SDK optional through the `mcp` installation extra.
- Support the locally available MCP 1.25 SDK and document offline editable installation.
- Use `D:\UVVis-Automation` for control-PC commands, methods, data, exports, and logs.
- Add in-process MCP schema and structured-call coverage.

## 0.4.0 - 2026-07-11

- Add dry-run-first repeated full Spectrum acquisition with start-to-start timing.
- Use a monotonic schedule and stop before a late measurement can overlap its predecessor.
- Generate unique data/export IDs plus per-run and per-series manifests.
- Register optional method scan speed and report nominal point/traverse timing.
- Add a control-PC growth-series wrapper and simulator acceptance coverage.
- Document old direct-controller settling, Spectrum timing, and Time Course boundaries.

## 0.3.0 - 2026-07-11

- Add registered Spectrum scan profiles with method, start, stop, and interval metadata.
- Add old-controller-compatible `--start`, `--stop`, and `--step` selection.
- Reject wavelength ranges that do not exactly match a validated LabSolutions method.
- Validate multiple target wavelengths against the selected range and data grid.
- Document continuous Spectrum versus discrete multi-wavelength Photometric operation.

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
