"""Demo 14: two-stage gearbox assembly built with MechKernel.

This is a presentation/evidence model rather than a manufacturing gear
generator.  Gears are represented by a round blank plus repeated rectangular
teeth, which keeps the example inside the public kernel API and makes the
parametric dimensions easy to inspect.
"""
from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mech_kernel import MechKernel

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


HERE = Path(__file__).parent
OUT = HERE / "gearbox_out"
OUT.mkdir(exist_ok=True)


def _kernel() -> MechKernel:
    kernel = MechKernel()
    # Component construction is intentionally silent/fast.  The demo renders
    # explicit evidence images after the whole scene exists.
    kernel.adaptive_renderer.suspended = True
    kernel.create_workplane("XY", "XY")
    return kernel


def _sketch(kernel: MechKernel, name: str) -> None:
    kernel.new_sketch("XY", name)


def _save_render(result, name: str) -> Path | None:
    if not result.render_base64:
        print(f"  [WARN] render empty: {name}")
        return None
    path = OUT / f"{name}.png"
    path.write_bytes(base64.b64decode(result.render_base64))
    print(f"  render: {path} ({path.stat().st_size} bytes)")
    return path


def _save_views(result, prefix: str) -> list[Path]:
    paths = []
    for view, png in (result.render_views or {}).items():
        if not png:
            continue
        path = OUT / f"{prefix}_{view}.png"
        path.write_bytes(png)
        paths.append(path)
    return paths


def _report(name: str, kernel: MechKernel) -> dict:
    volume = kernel.query("_current_geometry", "volume").value
    bbox = kernel.query("_current_geometry", "bounding_box").value
    summary = {
        "name": name,
        "volume_mm3": volume,
        "bbox": bbox,
        "feature_count": len(kernel.feature_graph.nodes),
    }
    print(
        f"  {name}: volume={volume:,.0f} mm3, "
        f"bbox={bbox['size_x']:.0f} x {bbox['size_y']:.0f} x {bbox['size_z']:.0f} mm, "
        f"features={summary['feature_count']}"
    )
    return summary


def _export(kernel: MechKernel, name: str) -> str:
    path = OUT / f"{name}.step"
    result = kernel.export(str(path), format="step")
    if not result.success:
        raise RuntimeError(f"STEP export failed for {name}: {result.error}")
    print(f"  step: {path} ({path.stat().st_size} bytes)")
    return str(path)


def build_housing() -> MechKernel:
    """Open-top housing assembled from a floor and four wall features."""
    k = _kernel()

    _sketch(k, "floor")
    k.add_rectangle("floor", width=180, height=120, center=(0, 0), name="housing_floor")
    k.close_sketch("floor")
    k.extrude("floor", depth=6, mode="new_body", name="housing_floor")

    walls = [
        ("wall_left", 6, 108, (-87, 0)),
        ("wall_right", 6, 108, (87, 0)),
        ("wall_front", 168, 6, (0, -57)),
        ("wall_back", 168, 6, (0, 57)),
    ]
    for name, width, height, center in walls:
        _sketch(k, name)
        k.add_rectangle(name, width=width, height=height, center=center, name=name)
        k.close_sketch(name)
        k.extrude(name, depth=84, mode="add", name=name)

    # Four mounting holes pass through the floor and remain visible in the
    # underside view.  The hole op is XY/Z oriented, matching this housing.
    for position in [(-72, -42), (72, -42), (-72, 42), (72, 42)]:
        k.hole(position=position, diameter=10, depth=12, name="mounting_hole")
    return k


def build_cover() -> MechKernel:
    k = _kernel()
    _sketch(k, "cover_plate")
    k.add_rectangle("cover_plate", width=180, height=120, center=(0, 0), name="cover")
    k.close_sketch("cover_plate")
    k.extrude("cover_plate", depth=6, mode="new_body", name="cover")
    for position in [(-72, -42), (72, -42), (-72, 42), (72, 42)]:
        k.hole(position=position, diameter=10, depth=12, name="cover_bolt_hole")
    return k


def build_shaft(length: float, diameter: float, name: str) -> MechKernel:
    k = _kernel()
    _sketch(k, f"{name}_profile")
    k.add_circle(f"{name}_profile", center=(0, 0), radius=diameter / 2, name=name)
    k.close_sketch(f"{name}_profile")
    k.extrude(f"{name}_profile", depth=length, mode="new_body", name=name)
    return k


def build_bearing(outer_radius: float, inner_radius: float, width: float, name: str) -> MechKernel:
    k = _kernel()
    _sketch(k, f"{name}_ring")
    k.add_circle(f"{name}_ring", center=(0, 0), radius=outer_radius)
    k.add_circle(f"{name}_ring", center=(0, 0), radius=inner_radius)
    k.close_sketch(f"{name}_ring")
    k.extrude(f"{name}_ring", depth=width, mode="new_body", name=name)
    return k


def build_gear(root_radius: float, teeth: int, width: float, bore: float, name: str) -> MechKernel:
    """Build a simple spur-gear visual proxy with a real center bore."""
    k = _kernel()
    _sketch(k, f"{name}_blank")
    k.add_circle(f"{name}_blank", center=(0, 0), radius=root_radius)
    k.add_circle(f"{name}_blank", center=(0, 0), radius=bore / 2)
    k.close_sketch(f"{name}_blank")
    # Two concentric circles use the kernel's native annular-cylinder path.
    k.extrude(f"{name}_blank", depth=width, mode="new_body", name=f"{name}_blank")

    _sketch(k, f"{name}_teeth")
    tooth_depth = max(3.0, root_radius * 0.12)
    tooth_width = max(2.5, 3.14159 * root_radius / teeth * 0.65)
    tooth_center = root_radius + tooth_depth * 0.5
    # Put all teeth into one sketch and extrude once.  This preserves the
    # public-kernel construction path while avoiding dozens of OCC fusions.
    for index in range(teeth):
        angle = 2.0 * math.pi * index / teeth
        cx = tooth_center * math.cos(angle)
        cy = tooth_center * math.sin(angle)
        k.add_rectangle(
            f"{name}_teeth",
            width=tooth_depth,
            height=tooth_width,
            center=(cx, cy),
            name=f"{name}_tooth_{index + 1:02d}",
        )
    k.close_sketch(f"{name}_teeth")
    # All teeth are extruded as one additive feature, avoiding N OCC fusions.
    k.extrude(f"{name}_teeth", depth=width, mode="add", name=f"{name}_teeth")
    return k


def build_end_cap() -> MechKernel:
    k = _kernel()
    _sketch(k, "end_cap")
    k.add_rectangle("end_cap", width=36, height=90, center=(0, 0), name="end_cap")
    k.close_sketch("end_cap")
    k.extrude("end_cap", depth=8, mode="new_body", name="end_cap")
    return k


def _rotated_part(path: str, name: str, x: float, y: float, z: float, color: list[float]) -> dict:
    # Source parts are made along Z.  A 90 degree Y rotation aligns their axis
    # with the gearbox X axis; x is the left/start placement of the source.
    return {
        "path": path,
        "position": [x, y, z],
        "rotation": [90, [0, 1, 0]],
        "name": name,
        "color": color,
        "visible": True,
    }


def build_assembly(paths: dict[str, str]) -> MechKernel:
    parts = [
        {"path": paths["housing"], "position": [-90, 0, 0], "name": "housing", "color": [0.22, 0.25, 0.29]},
        {"path": paths["cover"], "position": [-90, 0, 90], "name": "cover", "color": [0.58, 0.61, 0.66]},
        _rotated_part(paths["input_shaft"], "input_shaft", -99, -35, 35, [0.25, 0.42, 0.62]),
        _rotated_part(paths["intermediate_shaft"], "intermediate_shaft", -99, 0, 52, [0.25, 0.42, 0.62]),
        _rotated_part(paths["output_shaft"], "output_shaft", -99, 35, 70, [0.25, 0.42, 0.62]),
        _rotated_part(paths["gear_input"], "gear_input", -9, -35, 35, [0.84, 0.55, 0.18]),
        _rotated_part(paths["gear_intermediate_large"], "gear_intermediate_large", -37, 0, 52, [0.84, 0.55, 0.18]),
        _rotated_part(paths["gear_intermediate_small"], "gear_intermediate_small", 1, 0, 52, [0.92, 0.66, 0.24]),
        _rotated_part(paths["gear_output"], "gear_output", 20, 35, 70, [0.84, 0.55, 0.18]),
        _rotated_part(paths["bearing"], "bearing_input", -12, -35, 35, [0.72, 0.74, 0.77]),
        _rotated_part(paths["bearing"], "bearing_intermediate", -12, 0, 52, [0.72, 0.74, 0.77]),
        _rotated_part(paths["bearing"], "bearing_output", -12, 35, 70, [0.72, 0.74, 0.77]),
        {"path": paths["end_cap"], "position": [-98, 0, 45], "name": "input_end_cap", "color": [0.58, 0.61, 0.66]},
    ]
    k = MechKernel()
    result = k.assemble(parts, name="two_stage_gearbox")
    if not result.success:
        raise RuntimeError(f"assembly failed: {result.error}")
    return k


def _save_section_without_instance_scene(kernel: MechKernel, name: str) -> None:
    """Render the fused assembly section so the section plane affects all parts."""
    saved_scene = kernel._assembly_instances
    try:
        kernel._assembly_instances = {}
        result = kernel.render(
            intent="section",
            section={"axis": "Y", "offset": 0},
            views=["iso", "front", "side"],
            size=640,
            annotate=True,
            quality="evidence",
        )
        _save_render(result, name)
    finally:
        kernel._assembly_instances = saved_scene


def main() -> None:
    print("=" * 72)
    print("Demo 14: MechKernel two-stage gearbox presentation assembly")
    print("=" * 72)

    components = [
        ("housing", build_housing),
        ("cover", build_cover),
        ("input_shaft", lambda: build_shaft(198, 16, "input_shaft")),
        ("intermediate_shaft", lambda: build_shaft(198, 20, "intermediate_shaft")),
        ("output_shaft", lambda: build_shaft(198, 24, "output_shaft")),
        ("gear_input", lambda: build_gear(18, 20, 18, 16, "gear_input")),
        ("gear_intermediate_large", lambda: build_gear(52, 60, 18, 20, "gear_intermediate_large")),
        ("gear_intermediate_small", lambda: build_gear(16, 18, 18, 20, "gear_intermediate_small")),
        ("gear_output", lambda: build_gear(48, 54, 18, 24, "gear_output")),
        ("bearing", lambda: build_bearing(15, 9, 12, "bearing")),
        ("end_cap", build_end_cap),
    ]

    paths: dict[str, str] = {}
    reports = []
    for name, builder in components:
        print(f"\n[part] {name}")
        kernel = builder()
        reports.append(_report(name, kernel))
        paths[name] = _export(kernel, name)

    print("\n[assembly] two-stage gearbox")
    assembly = build_assembly(paths)
    reports.append(_report("two_stage_gearbox", assembly))
    assembly_step = _export(assembly, "two_stage_gearbox")

    full = assembly.render(
        views=["iso", "front", "top", "side"],
        size=640,
        annotate=True,
        quality="evidence",
        show_edges=True,
    )
    _save_render(full, "gearbox_full_evidence")
    _save_views(full, "gearbox_full")

    # Hide the two shell parts to expose shafts, bearings and gear stages.
    assembly.set_instance_visibility("A_0001", False)
    assembly.set_instance_visibility("A_0002", False)
    interior = assembly.render(
        views=["iso", "front", "top", "side"],
        size=640,
        annotate=True,
        quality="evidence",
        highlight=["A_008", "A_009"],
    )
    _save_render(interior, "gearbox_interior_evidence")
    _save_views(interior, "gearbox_interior")

    _save_section_without_instance_scene(assembly, "gearbox_section_evidence")

    # Restore the complete scene before the presentation render.
    assembly.set_instance_visibility("A_0001", True)
    assembly.set_instance_visibility("A_0002", True)
    presentation = assembly.render(
        views=["iso"],
        size=1280,
        annotate=True,
        quality="presentation",
        show_edges=True,
    )
    _save_render(presentation, "gearbox_presentation")

    turntable = assembly.render(turntable=True, size=480, annotate=True, quality="evidence")
    _save_render(turntable, "gearbox_turntable")

    query = assembly.query_assembly()
    manifest = {
        "parts": reports,
        "assembly_step": assembly_step,
        "scene": query.value,
        "render_backend": {
            "full": full.backend_used,
            "interior": interior.backend_used,
            "presentation": presentation.backend_used,
        },
    }
    (OUT / "gearbox_report.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nreport: {OUT / 'gearbox_report.json'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
