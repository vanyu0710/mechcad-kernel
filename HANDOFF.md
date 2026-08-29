# MechKernel Handoff

Updated: 2026-08-29

## Current Repository State

- Repository: `https://github.com/vanyu0710/mechcad-kernel.git`
- Branch: `main`
- Last stable commit before this handoff: `7a44510 feat: add v2.6 geometry validation and rollback`
- v2.6 baseline test result: `253 passed`
- The v2.7 reference-coordinate implementation was discussed but **not started**.
- No API keys, `.env` files, VPN settings, or provider credentials are committed.

This handoff commit adds the reproducible gearbox demo and this document. Generated
STEP/PNG/report files remain ignored under `mech_kernel/examples/gearbox_out/`.

## Implemented Features

The stable v2.6 baseline contains:

- Parameterized op history and full replay for in-session modeling.
- `delete_feature`, `update_feature`, and `rebuild` with real recomputation.
- Undo/redo snapshots that retain in-process geometry references.
- Multi-view render evidence, section rendering, adaptive rendering, and renderer
  backend manifest/fallback reporting.
- Instance-level assembly metadata: visibility, color, scene manifest, and STEP
  fusion through `assemble(parts)`.
- Constraint data structures and the SciPy-based sketch constraint solver.
- Deterministic geometry validation, fingerprints, strict transaction validation,
  and rollback via `validate_geometry`.
- OpenAI-compatible planner/vision clients with environment-only secret handling.

## Demo 14 Status

`mech_kernel/examples/14_gearbox.py` builds and exports:

- Housing and cover.
- Input, intermediate, and output shafts.
- Four visual proxy spur gears.
- Bearings and end cap.
- Fused assembly STEP.
- Full, interior, section, presentation, and turntable PNG evidence.
- `gearbox_report.json` with component and scene metadata.

The demo was executed successfully and its component/assembly outputs were
generated locally. It is a **展示级渲染 Demo**, not yet a mechanically validated
gearbox. Known limitations are intentional to address in v2.7:

- Part placement still uses hard-coded world `position`/`rotation` values.
- The cover and several mounting references do not use a shared assembly datum.
- Gear center distances are not derived from a common module/pitch specification.
- Shafts, gears, bearings, and housing have no semantic coaxial/mount relations.
- `assemble()` applies transforms and fuses STEP geometry, but does not diagnose
  gear interference, axis mismatch, bearing fit, or cover seating.
- The current environment used the matplotlib renderer fallback; OCC native
  rendering was not active.

Do not use the Demo 14 output as evidence of production-ready gearbox kinematics.

## Next Development Target: v2.7

Implement the following in order:

1. Add a generic `CoordinateFrame`/reference-plane module with origin, normal,
   x-axis, derived y-axis, parent frame, serialization, and deterministic
   right-handed normalization.
2. Add public `create_reference_plane()` and `query_reference()` operations.
3. Add `resolve_point()` and `resolve_placement()` for `{frame, uv,
   normal_offset}` values while preserving old absolute tuple/list inputs.
4. Store reference frames in snapshots, graph/history persistence, and replay.
5. Extend assembly instances with local origin, mount frame, resolved world
   transform, and world bbox. Keep old `assemble(parts)` syntax compatible.
6. Add public `validate_assembly(level, relations)` with generic checks for
   frame validity, coaxial/parallel/perpendicular relations, clearances,
   mounting, containment, and optional gear mesh relations.
7. Rewrite Demo 14 to calculate gear pitch diameters and center distances from
   shared parameters and place every component through reference planes.
8. Add focused tests before changing presentation output; then rerun the full
   suite and regenerate the Demo 14 evidence/report.

Recommended public relation shape:

```json
{
  "kind": "coaxial|parallel|perpendicular|mounted|inside|clearance|gear_mesh",
  "source": "input_shaft",
  "target": "gear_input",
  "parameters": {}
}
```

Recommended relative coordinate shape:

```json
{
  "frame": "housing_mount_plane",
  "uv": [35, 20],
  "normal_offset": 6
}
```

## Verification Commands

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe mech_kernel\examples\14_gearbox.py
git diff --check
git status --short
```

The expected v2.6 baseline is `253 passed`. The Demo output directory is ignored
and should not be force-added to Git.

## Git/Push Notes

- Use the local Clash/Mihomo HTTP proxy at `http://127.0.0.1:7897` only for a
  network command when direct HTTPS is unavailable.
- Do not save that proxy in repository or global Git configuration.
- The intended push command is:

```powershell
git -c http.proxy=http://127.0.0.1:7897 `
    -c https.proxy=http://127.0.0.1:7897 `
    -c http.version=HTTP/1.1 push origin main
```

- After pushing, verify with `git status --short`, `git log -1 --oneline`, and
  `git ls-remote origin refs/heads/main` using the same one-shot proxy options.

## Security Notes

- Keep real credentials only in a local `.env` or process environment.
- `.env.example` contains variable names only.
- Planner and vision clients redact API keys from `repr`, logs, exceptions, and
  serialized project data.
- Never paste a real key into this handoff or commit it to Git.
