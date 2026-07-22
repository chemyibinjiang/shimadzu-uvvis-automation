# Changelog

## Unreleased

- Route mode-agnostic requests before any LabSolutions or instrument action. Supported
  range intervals remain Spectrum scans; unsupported intervals such as 10 nm become
  exact Photometric wavelength lists instead of failing after Spectrum starts.
- Add automatic structural routing for discrete wavelengths, fixed-wavelength time
  courses, and quantitation-purpose requests while retaining explicit mode selection.
- Add a Windows `LabSolutionsRuntimeManager` that launches Spectrum, validates or
  sets the Automatic Control directory, enters Waiting, and requires a successful
  `Command=0` / `Return=0` handshake before reporting `READY`.
- Automatically decline LabSolutions' parameter-change baseline prompt through
  stable Win32 control IDs after method loading; keep physical baseline correction
  exclusively behind the guarded `Command=21` batch step.
- Gate batch start, baseline correction, and every sample measurement on runtime
  readiness. Only batch start may change runtime settings; physical phases only
  verify and stop on mismatch.
- Archive failed side-effect-free Hello probes while refusing to clean or retry
  recovery records for any physical command.
- Add guarded `start_uvvis_batch`, `correct_uvvis_baseline`,
  `measure_next_uvvis_sample`, `get_uvvis_batch_status`, and
  `abort_uvvis_batch` MCP tools for Spectrum.
- Persist batch state, baseline identity, exact sample order, command feedback,
  raw data metadata, and archived export metadata in atomic JSON manifests.
- Hold the LabSolutions workflow lock across grouped batch commands and stop in
  `RECOVERY_REQUIRED` after ambiguous command or output failures.
- Parse Spectrum X/Y streams directly from `.vspd` with automatic-export fallback,
  validate the exact requested wavelength grid, create normalized CSV/JSON/PNG
  results, and require repository publication before completing a sample.
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
