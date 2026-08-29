"""Small, deterministic productivity benchmark runner.

The runner intentionally uses the public Kernel API only.  It is suitable for
CI and for comparing planners without exposing provider credentials.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from mech_kernel import MechKernel


@dataclass
class BenchmarkRecord:
    part: str
    task: str
    success: bool
    elapsed_ms: float
    op_count: int = 0
    solve_ms: float = 0.0
    visual_calls: int = 0
    evidence_pixels: int = 0
    token_estimate: int = 0
    volume_before: Optional[float] = None
    volume_after: Optional[float] = None
    volume_relative_error: Optional[float] = None
    bbox_error: Optional[float] = None
    validation_status: Optional[str] = None
    geometry_fingerprint: Optional[str] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def _volume(kernel: MechKernel) -> Optional[float]:
    try:
        return float(kernel.query("_current_geometry", "volume").value)
    except Exception:
        return None


def _bbox(kernel: MechKernel) -> Optional[List[float]]:
    try:
        value = kernel.query("_current_geometry", "bounding_box").value
        if isinstance(value, dict):
            return [float(value[k]) for k in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")]
        return [float(v) for v in value]
    except Exception:
        return None


def _build_cylinder(
    kernel: MechKernel, radius: float = 20.0, depth: float = 40.0,
    named: bool = True,
) -> str:
    kernel.create_workplane("base")
    kernel.new_sketch("base", "main")
    circle = kernel.add_circle("main", (0, 0), radius, name="outer_radius")
    if named:
        # Constraint solving is optional for geometry-only benchmark runs.
        # Complex examples must still run in lightweight/no-SciPy installs.
        kernel.add_constraint("main", "radius", [{"entity_id": circle.feature_id, "role": "circle"}],
                              value=radius, parameter_name="outer_radius")
    kernel.close_sketch("main")
    feature = kernel.extrude("main", depth, name="body")
    return feature.feature_id


def _build_flange(kernel: MechKernel) -> str:
    return _build_cylinder(kernel, radius=30.0, depth=8.0)


def _build_stepped_shaft(kernel: MechKernel) -> str:
    kernel.create_workplane("base")
    kernel.new_sketch("base", "shaft")
    kernel.add_rectangle("shaft", 20, 20, name="shaft_section")
    kernel.close_sketch("shaft")
    return kernel.extrude("shaft", 80, name="shaft_body").feature_id


def _build_l_bracket(kernel: MechKernel) -> str:
    kernel.create_workplane("base")
    kernel.new_sketch("base", "bracket")
    kernel.add_rectangle("bracket", 60, 10, center=(0, 25), name="horizontal_leg")
    kernel.add_rectangle("bracket", 10, 50, center=(25, 0), name="vertical_leg")
    kernel.close_sketch("bracket")
    return kernel.extrude("bracket", 10, name="bracket_body").feature_id


def _build_holed_housing(kernel: MechKernel) -> str:
    feature_id = _build_cylinder(kernel, radius=25, depth=40)
    kernel.hole(position=(0, 0), diameter=12, depth=40, name="bore")
    return feature_id


def _build_patterned_plate(kernel: MechKernel) -> str:
    kernel.create_workplane("base")
    kernel.new_sketch("base", "plate")
    kernel.add_rectangle("plate", 80, 50, name="plate_outline")
    kernel.close_sketch("plate")
    feature_id = kernel.extrude("plate", 8, name="plate_body").feature_id
    kernel.new_sketch("base", "hole")
    kernel.add_circle("hole", (-25, -15), 3, name="hole_seed")
    kernel.close_sketch("hole")
    kernel.linear_pattern("hole", count=4, direction=(1, 0), spacing=16, mode="cut", name="hole_array")
    return feature_id


BUILDERS: Dict[str, Callable[[MechKernel], str]] = {
    "flange": _build_flange,
    "stepped_shaft": _build_stepped_shaft,
    "l_bracket": _build_l_bracket,
    "holed_housing": _build_holed_housing,
    "patterned_plate": _build_patterned_plate,
    "rocket_motor_shell": lambda k: _build_cylinder(k, 30, 120, named=False),
    "rocket_nozzle": lambda k: _build_cylinder(k, 24, 35, named=False),
    "rocket_closure": lambda k: _build_cylinder(k, 30, 15, named=False),
}


def _record_visual(record: BenchmarkRecord, result: Any) -> None:
    record.visual_calls += 1
    manifest = getattr(result, "evidence_manifest", None) or {}
    layout = manifest.get("layout", {}) if isinstance(manifest, dict) else {}
    size = layout.get("max_size_px", 0)
    record.evidence_pixels = max(record.evidence_pixels, int(size or 0) ** 2)
    # A conservative estimate for image input: 1 token per ~1K pixels.
    record.token_estimate += max(1, record.evidence_pixels // 1000) if record.evidence_pixels else 0


def _run_one(part: str, task: str) -> BenchmarkRecord:
    started = time.perf_counter()
    record = BenchmarkRecord(part=part, task=task, success=False, elapsed_ms=0.0)
    try:
        if part not in BUILDERS:
            raise ValueError(f"no builder for part {part}")
        kernel = MechKernel()
        base_feature = BUILDERS[part](kernel)
        record.op_count = len(kernel._op_history)
        record.volume_before = _volume(kernel)
        original_bbox = _bbox(kernel)

        if task == "create":
            result = kernel.query("_current_geometry", "volume")
        elif task == "modify_named_dimension":
            result = kernel.set_parameter("outer_radius", 22.0) if "outer_radius" in kernel._parameters else kernel.rebuild()
            record.volume_after = _volume(kernel)
        elif task == "delete_feature":
            result = kernel.delete_feature(base_feature)
            record.volume_after = _volume(kernel)
        elif task == "rebuild":
            result = kernel.rebuild()
            record.volume_after = _volume(kernel)
        elif task == "save_load":
            with tempfile.TemporaryDirectory(prefix="mechcad_bench_") as temp_dir:
                paths = kernel.save_project(os.path.join(temp_dir, part))
                loaded = MechKernel().load_project(os.path.join(temp_dir, part))
                result = loaded
                record.details["history_size"] = paths.get("history_size", 0)
        elif task == "visual_evidence":
            result = kernel.render(size=256)
            _record_visual(record, result)
            record.details["views"] = list(result.render_views or {})
        elif task == "validate":
            result = kernel.validate_geometry(level="strict")
            validation = result.geometry_validation or {}
            record.validation_status = validation.get("status")
            record.geometry_fingerprint = validation.get("fingerprint")
        else:
            raise ValueError(f"unknown task {task}")

        record.success = bool(getattr(result, "success", False))
        if not record.success:
            record.error = getattr(result, "error", "operation failed")
        after = record.volume_after if record.volume_after is not None else _volume(kernel)
        if record.volume_before is not None and after is not None:
            record.volume_after = after
            record.volume_relative_error = abs(after - record.volume_before) / max(abs(record.volume_before), 1e-12)
        if original_bbox and _bbox(kernel):
            record.bbox_error = max(abs(a - b) for a, b in zip(original_bbox, _bbox(kernel)))
        record.op_count = max(record.op_count, len(kernel._op_history))
        if record.geometry_fingerprint is None:
            validation = getattr(result, "geometry_validation", None) or {}
            if not validation and kernel._current_geometry is not None:
                validation = kernel.geometry_inspector.validate_geometry(
                    kernel._current_geometry, level="standard"
                ).to_dict()
            record.validation_status = validation.get("status")
            record.geometry_fingerprint = validation.get("fingerprint")
        record.solve_ms = float(getattr(result, "elapsed_ms", 0.0)) if task == "modify_named_dimension" else 0.0
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {exc}"
    record.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return record


def _aggregate(records: Iterable[BenchmarkRecord]) -> Dict[str, Any]:
    records = list(records)
    successful = [r for r in records if r.success]
    return {
        "task_count": len(records),
        "success_count": len(successful),
        "success_rate": len(successful) / len(records) if records else 0.0,
        "average_elapsed_ms": sum(r.elapsed_ms for r in records) / len(records) if records else 0.0,
        "average_op_count": sum(r.op_count for r in records) / len(records) if records else 0.0,
        "average_solve_ms": sum(r.solve_ms for r in records) / len(records) if records else 0.0,
        "average_evidence_pixels": sum(r.evidence_pixels for r in records) / len(records) if records else 0.0,
        "visual_calls": sum(r.visual_calls for r in records),
        "token_estimate": sum(r.token_estimate for r in records),
    }


def run_suite(parts: Optional[Iterable[str]] = None, tasks: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Run the public-API productivity suite and return JSON-safe results."""
    selected_parts = list(parts or BUILDERS.keys())
    selected_tasks = list(tasks or ("create", "modify_named_dimension", "delete_feature", "rebuild", "save_load", "visual_evidence", "validate"))
    records = [_run_one(part, task) for part in selected_parts for task in selected_tasks]
    return {
        "schema_version": "2.6",
        "records": [asdict(record) for record in records],
        "summary": _aggregate(records),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", nargs="*", default=None)
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_suite(args.parts, args.tasks)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["summary"]["success_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
