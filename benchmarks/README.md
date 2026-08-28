# MechCAD productivity benchmarks

Run from the repository root:

```text
python -m benchmarks.run --output reports/v2.4.json
```

The runner uses only the public `MechKernel` API. It reports first-build,
named-dimension modification, feature deletion, rebuild, save/load, and
multi-view evidence tasks. `cases.json` is the versioned task manifest; the
JSON report is safe to archive because it contains no provider configuration
or API credentials.

The initial builders are intentionally small and deterministic. They provide a
repeatable baseline for comparing planners. More elaborate rocket motor
assembly tasks can be added without changing the report schema.
