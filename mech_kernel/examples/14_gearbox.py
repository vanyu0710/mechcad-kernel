"""
Demo 14 v2.7: Two-stage gearbox assembly built with reference-coordinate frames.

v2.7 重写：所有部件放置基于共享 reference plane；齿轮中心距、轴向位置
由 module / 齿数参数计算；用 validate_assembly 校验共轴 + 齿轮啮合关系。
"""
from __future__ import annotations

import base64
import json
import math
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mech_kernel import MechKernel
from mech_kernel.reference_frames import resolve_point, resolve_placement

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


HERE = Path(__file__).parent
OUT = HERE / "gearbox_out"
OUT.mkdir(exist_ok=True)


# ============================================================
# 1) 全局参数（齿轮 module + 齿数 → 中心距自动算）
# ============================================================

GEAR_PARAMS = {
    "module": 2.0,                    # 齿轮模数 m
    "pressure_angle_deg": 20.0,
    "face_width": 18.0,
    "bore_clearance": 1.0,            # 齿轮孔 vs 轴的间隙

    # 各级齿数 (input, intermediate, output)
    "z_input": 20,
    "z_intermediate_large": 60,
    "z_intermediate_small": 18,
    "z_output": 54,

    # 轴中心距
    # 第一级: input shaft ↔ intermediate shaft
    # 第二级: intermediate shaft ↔ output shaft
    # 由 pitch_diameter = m * z 推导
}


def _pitch_radius(module: float, teeth: int) -> float:
    return module * teeth / 2.0


def _center_distance(m: float, z1: int, z2: int) -> float:
    return m * (z1 + z2) / 2.0


def compute_geometry() -> dict:
    """从 GEAR_PARAMS 推出所有尺寸."""
    p = GEAR_PARAMS
    m = p["module"]
    return {
        # 齿轮 pitch radius
        "r_pitch_input": _pitch_radius(m, p["z_input"]),
        "r_pitch_intermediate_large": _pitch_radius(m, p["z_intermediate_large"]),
        "r_pitch_intermediate_small": _pitch_radius(m, p["z_intermediate_small"]),
        "r_pitch_output": _pitch_radius(m, p["z_output"]),

        # 中心距
        "center_distance_stage1": _center_distance(m, p["z_input"], p["z_intermediate_large"]),
        "center_distance_stage2": _center_distance(m, p["z_intermediate_small"], p["z_output"]),

        # face width
        "face_width": p["face_width"],

        # 齿轮 bore (= 轴直径 + clearance)
        "bore_input": 16 + p["bore_clearance"],          # shaft Ø16
        "bore_intermediate": 20 + p["bore_clearance"],
        "bore_output": 24 + p["bore_clearance"],

        # housing (足够大, 覆盖 input shaft -100~100 + output shaft -50~150)
        "housing_length": 300.0,    # X: 覆盖 input + output 总 span
        "housing_width": 180.0,     # Y: 容纳 intermediate Y=57 + gear radius 62 + buffer
        "housing_height": 90.0,
        "wall_thickness": 6.0,

        # shafts (200mm, 居中)
        "shaft_length": 200.0,
        "shaft_input_d": 16.0,
        "shaft_intermediate_d": 20.0,
        "shaft_output_d": 24.0,

        # bearing
        "bearing_outer": 30.0,
        "bearing_inner": 18.0,
        "bearing_width": 12.0,
    }


# ============================================================
# 2) 零件构造（保持原 demo 14 的视觉代理风格）
# ============================================================

def _kernel() -> MechKernel:
    kernel = MechKernel()
    kernel.adaptive_renderer.suspended = True
    kernel.create_workplane("XY", "XY")
    return kernel


def _sketch(kernel: MechKernel, name: str) -> None:
    kernel.new_sketch("XY", name)


def _save_render(result, name: str) -> Path | None:
    if not getattr(result, "render_base64", None):
        print(f"  [WARN] render empty: {name}")
        return None
    path = OUT / f"{name}.png"
    path.write_bytes(base64.b64decode(result.render_base64))
    print(f"  render: {path} ({path.stat().st_size} bytes)")
    return path


def _save_views(result, prefix: str) -> list[Path]:
    paths = []
    for view, png in (getattr(result, "render_views", None) or {}).items():
        if not png:
            continue
        path = OUT / f"{prefix}_{view}.png"
        if isinstance(png, str):
            path.write_bytes(base64.b64decode(png))
        else:
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
        f"bbox={bbox['size_x']:.0f} x {bbox['size_y']:.0f} x {bbox['size_z']:.0f} mm"
    )
    return summary


def _export(kernel: MechKernel, name: str) -> str:
    path = OUT / f"{name}.step"
    result = kernel.export(str(path), format="step")
    if not result.success:
        raise RuntimeError(f"STEP export failed for {name}: {result.error}")
    print(f"  step: {path} ({path.stat().st_size} bytes)")
    return str(path)


def build_housing(geom: dict) -> MechKernel:
    k = _kernel()
    _sketch(k, "floor")
    k.add_rectangle("floor", width=geom["housing_length"], height=geom["housing_width"],
                    center=(0, 0), name="housing_floor")
    k.close_sketch("floor")
    k.extrude("floor", depth=geom["wall_thickness"], mode="new_body", name="housing_floor")

    walls = [
        ("wall_left", geom["wall_thickness"], geom["housing_height"] - 2 * geom["wall_thickness"],
         (-(geom["housing_length"] - geom["wall_thickness"]) / 2, 0)),
        ("wall_right", geom["wall_thickness"], geom["housing_height"] - 2 * geom["wall_thickness"],
         ((geom["housing_length"] - geom["wall_thickness"]) / 2, 0)),
        ("wall_front", geom["housing_length"], geom["wall_thickness"],
         (0, -(geom["housing_width"] - geom["wall_thickness"]) / 2)),
        ("wall_back", geom["housing_length"], geom["wall_thickness"],
         (0, (geom["housing_width"] - geom["wall_thickness"]) / 2)),
    ]
    for name, width, height, center in walls:
        _sketch(k, name)
        k.add_rectangle(name, width=width, height=height, center=center, name=name)
        k.close_sketch(name)
        k.extrude(name, depth=geom["housing_height"] - geom["wall_thickness"], mode="add", name=name)
    return k


def build_cover(geom: dict) -> MechKernel:
    k = _kernel()
    _sketch(k, "cover_plate")
    k.add_rectangle("cover_plate", width=geom["housing_length"], height=geom["housing_width"],
                    center=(0, 0), name="cover")
    k.close_sketch("cover_plate")
    k.extrude("cover_plate", depth=geom["wall_thickness"], mode="new_body", name="cover")
    return k


def build_shaft(length: float, diameter: float, name: str) -> MechKernel:
    k = _kernel()
    _sketch(k, f"{name}_profile")
    k.add_circle(f"{name}_profile", center=(0, 0), radius=diameter / 2, name=name)
    k.close_sketch(f"{name}_profile")
    k.extrude(f"{name}_profile", depth=length, mode="new_body", name=name)
    return k


def build_bearing(geom: dict, name: str) -> MechKernel:
    k = _kernel()
    _sketch(k, f"{name}_ring")
    k.add_circle(f"{name}_ring", center=(0, 0), radius=geom["bearing_outer"] / 2)
    k.add_circle(f"{name}_ring", center=(0, 0), radius=geom["bearing_inner"] / 2)
    k.close_sketch(f"{name}_ring")
    k.extrude(f"{name}_ring", depth=geom["bearing_width"], mode="new_body", name=name)
    return k


def build_gear(module: float, teeth: int, width: float, bore: float, name: str) -> MechKernel:
    """v2.8 真实数学齿轮 (trapezoidal proxy + 正确 ISO 几何参数).

    使用 build_involute_gear(module, teeth, width, bore):
    - pitch_radius = m*z/2 (ISO 6336-1)
    - addendum_radius = r + m
    - dedendum_radius = r - 1.25m
    - tooth 形: 梯形 (顶宽 50% 全宽, 跟宽 100%)
    """
    from mech_kernel.gear import build_involute_gear as _gen
    from mech_kernel.features import FeatureNode, FeatureType, FeatureState, next_feature_id
    part = _gen(module=module, teeth=teeth, width=width, bore=bore)

    k = _kernel()
    # 简化: 直接设 _current_geometry, 跳过 kernel 内 API
    k._current_geometry = part
    fid = next_feature_id()
    feat = FeatureNode(
        id=fid, type=FeatureType.EXTRUDE,
        parameters={"module": module, "teeth": teeth, "width": width, "bore": bore, "source": "build_involute_gear"},
        name=f"{name}_involute", state=FeatureState.COMPUTED,
    )
    k.feature_graph.add(feat)
    k.narrative.append(f"build_involute_gear module={module} teeth={teeth} width={width} bore={bore}")
    return k


def build_end_cap(geom: dict) -> MechKernel:
    k = _kernel()
    _sketch(k, "end_cap")
    k.add_rectangle("end_cap", width=36, height=90, center=(0, 0), name="end_cap")
    k.close_sketch("end_cap")
    k.extrude("end_cap", depth=8, mode="new_body", name="end_cap")
    return k


# ============================================================
# 3) Reference Plane 装配
# ============================================================

# 装配坐标系约定:
#   X = gearbox length (shaft axis)
#   Y = housing width
#   Z = housing height
# 输入轴在 X 起点附近, 中间轴在 housing 中部, 输出轴在 X 末端附近.

# 三个轴的 X 位置: 0 (input), +offset_stage1 (intermediate), +offset_stage2 (output)
# 轴的 Y 位置: 三根轴分布在 housing 宽度的 1/4, 1/2, 3/4 处
# 轴的 Z 位置: 三根轴分布在 housing 高度的 1/4, 1/2, 3/4 处

# 中心距 (center_distance_stage1) 决定 input ↔ intermediate 在 XY 平面投影
# 中心距 (center_distance_stage2) 决定 intermediate ↔ output

def setup_reference_frames(kernel: MechKernel, geom: dict) -> dict:
    """在 kernel 内创建一组 reference plane, 返回 frame 名称字典.

    Inline 2 段减速器布局 (3 根轴都沿 X 方向, 但 X 位置不同):
      - input_shaft  : (0,        0, z_floor) 沿 X
      - intermediate : (X_mid,  Y_offset, z_floor) 沿 X  (中间轴偏置)
      - output_shaft : (X_end,    0, z_floor) 沿 X

    几何推导:
      X_mid² + Y_offset² = cd1² = 80² = 6400   (input ↔ intermediate)
      (X_mid - X_end)² + Y_offset² = cd2² = 72² = 5184  (intermediate ↔ output)

      选 X_end = 100, 解: X_mid = 56.08, Y_offset = 57.06
    """
    import math as _math
    p = GEAR_PARAMS
    cd1 = geom["center_distance_stage1"]   # 80
    cd2 = geom["center_distance_stage2"]   # 72

    # 选 X_end (output 距 input 的 X 距离), 解 X_mid 和 Y_offset
    # 选 X_end=60, 中间轴在 X_mid=36.7, Y=70.6 (更紧凑)
    X_end = 60.0
    # X_mid² - (X_mid - X_end)² = cd1² - cd2²
    X_mid = (cd1**2 - cd2**2 + X_end**2) / (2 * X_end)
    Y_offset = _math.sqrt(cd1**2 - X_mid**2)

    input_x = 0.0
    inter_x = X_mid
    inter_y = Y_offset
    output_x = X_end

    z_floor = geom["wall_thickness"] + 10
    shaft_z = z_floor
    input_z = shaft_z
    inter_z = shaft_z
    output_z = shaft_z

    # World root
    kernel.create_reference_plane("world", origin=(0, 0, 0), normal=(0, 1, 0), x_axis=(1, 0, 0),
                                  metadata={"role": "root"})
    kernel.create_reference_plane("housing_mount_plane",
                                  origin=(0, 0, 0), normal=(0, 1, 0), x_axis=(1, 0, 0),
                                  parent="world",
                                  metadata={"role": "housing_datum"})
    # 三根轴 axis frame (沿 X 方向)
    kernel.create_reference_plane("input_shaft_axis",
                                  origin=(input_x, 0, input_z),
                                  normal=(1, 0, 0), x_axis=(0, 0, 1),
                                  parent="housing_mount_plane",
                                  metadata={"role": "axis", "shaft": "input"})
    kernel.create_reference_plane("intermediate_shaft_axis",
                                  origin=(inter_x, inter_y, inter_z),
                                  normal=(1, 0, 0), x_axis=(0, 0, 1),
                                  parent="housing_mount_plane",
                                  metadata={"role": "axis", "shaft": "intermediate",
                                            "y_offset": inter_y, "x_offset": inter_x})
    kernel.create_reference_plane("output_shaft_axis",
                                  origin=(output_x, 0, output_z),
                                  normal=(1, 0, 0), x_axis=(0, 0, 1),
                                  parent="housing_mount_plane",
                                  metadata={"role": "axis", "shaft": "output"})

    return {
        "input_x": input_x,
        "inter_x": inter_x, "inter_y": inter_y,
        "output_x": output_x,
        "input_y": 0, "output_y": 0,
        "shaft_z": shaft_z,
        "input_z": input_z, "inter_z": inter_z, "output_z": output_z,
    }


# ============================================================
# 4) 通过 reference plane 计算 instance position/rotation
# ============================================================

def _placement_from_frame(kernel: MechKernel, frame: str,
                          uv=(0, 0), normal_offset=0.0,
                          rotation=(0, (0, 0, 1))) -> tuple:
    """调 resolve_placement, 返回 (world_position, [angle, axis])."""
    r = kernel.resolve_placement(frame, uv=uv, normal_offset=normal_offset, rotation=rotation)
    return r.value["origin"], [r.value["rotation"][0], r.value["rotation"][1]]


# 简化策略: shaft 原点沿其 axis frame 的 +x 方向走, 即 normal 方向 (frame.normal = (1,0,0))
# 因此 shaft = (uv=(0,0), normal_offset=length/2) 中心化放置
# gear 相同, 但 uv 偏移 0 (同心), normal_offset = 沿轴放
# housing 在 housing_mount_plane 上, uv 中心, normal_offset = 0
# bearing 在 input_shaft_axis 上, normal_offset = 沿轴向 (0 起, +x)

def build_assembly(kernel: MechKernel, paths: dict, geom: dict, layout: dict) -> MechKernel:
    """基于 reference plane 计算每个 instance 的 position/rotation, 用 mount_frame 关联."""
    m = GEAR_PARAMS["module"]
    shaft_len = geom["shaft_length"]

    parts = []
    # 1. Housing: 在 housing_mount_plane 原点
    parts.append({
        "path": paths["housing"],
        "name": "housing",
        "color": [0.22, 0.25, 0.29],
        "visible": True,
        "mount_frame": "housing_mount_plane",
        "mount_uv": [0, 0],
        "mount_normal_offset": 0.0,
    })

    # 2. Cover: housing 上方
    parts.append({
        "path": paths["cover"],
        "name": "cover",
        "color": [0.58, 0.61, 0.66],
        "visible": True,
        "mount_frame": "housing_mount_plane",
        "mount_uv": [0, 0],
        "mount_normal_offset": geom["housing_height"] - geom["wall_thickness"],
    })

    # 3. Input shaft: 沿 input_shaft_axis 居中放置 (normal_offset=0)
    # source shaft 是沿 Z 方向. 旋转 90° 让它沿 frame.normal (世界 X) 方向.
    # normal_offset=0 表示 shaft 中心在 frame origin, shaft 沿 ±shaft_len/2 方向
    input_shaft_pos, _ = _placement_from_frame(
        kernel, "input_shaft_axis",
        uv=(0, 0), normal_offset=0,
        rotation=(90, (0, 1, 0)),
    )
    parts.append({
        "path": paths["input_shaft"],
        "position": input_shaft_pos,
        "rotation": [90, [0, 1, 0]],
        "name": "input_shaft",
        "color": [0.25, 0.42, 0.62],
        "visible": True,
        "mount_frame": "input_shaft_axis",
    })

    # 4. Intermediate shaft (居中)
    inter_shaft_pos, _ = _placement_from_frame(
        kernel, "intermediate_shaft_axis",
        uv=(0, 0), normal_offset=0,
        rotation=(90, (0, 1, 0)),
    )
    parts.append({
        "path": paths["intermediate_shaft"],
        "position": inter_shaft_pos,
        "rotation": [90, [0, 1, 0]],
        "name": "intermediate_shaft",
        "color": [0.25, 0.42, 0.62],
        "visible": True,
        "mount_frame": "intermediate_shaft_axis",
    })

    # 5. Output shaft (居中)
    output_shaft_pos, _ = _placement_from_frame(
        kernel, "output_shaft_axis",
        uv=(0, 0), normal_offset=0,
        rotation=(90, (0, 1, 0)),
    )
    parts.append({
        "path": paths["output_shaft"],
        "position": output_shaft_pos,
        "rotation": [90, [0, 1, 0]],
        "name": "output_shaft",
        "color": [0.25, 0.42, 0.62],
        "visible": True,
        "mount_frame": "output_shaft_axis",
    })

    # 6-9. 4 个齿轮（与轴同 frame, 不同 normal_offset 决定轴向位置）
    # 啮合对 X 位置必须相同:
    #   input gear ↔ intermediate_large (X=30)
    #   intermediate_small ↔ output gear (X=90)
    for gear_name, axis_frame, axis_offset in [
        ("gear_input", "input_shaft_axis", 30.0),
        ("gear_intermediate_large", "intermediate_shaft_axis", 30.0),
        ("gear_intermediate_small", "intermediate_shaft_axis", 90.0),
        ("gear_output", "output_shaft_axis", 90.0),
    ]:
        pos, _ = _placement_from_frame(
            kernel, axis_frame, uv=(0, 0), normal_offset=axis_offset,
            rotation=(90, (0, 1, 0)),
        )
        parts.append({
            "path": paths[gear_name],
            "position": pos,
            "rotation": [90, [0, 1, 0]],
            "name": gear_name,
            "color": [0.84, 0.55, 0.18],
            "visible": True,
            "mount_frame": axis_frame,
        })

    # 10-12. 3 套轴承（每根轴 1 个, 在轴末端附近, 但仍在 shaft 上）
    for bearing_name, axis_frame, axis_offset in [
        ("bearing_input", "input_shaft_axis", 80.0),
        ("bearing_intermediate", "intermediate_shaft_axis", 80.0),
        ("bearing_output", "output_shaft_axis", 80.0),
    ]:
        pos, _ = _placement_from_frame(
            kernel, axis_frame, uv=(0, 0), normal_offset=axis_offset,
            rotation=(90, (0, 1, 0)),
        )
        parts.append({
            "path": paths["bearing"],
            "position": pos,
            "rotation": [90, [0, 1, 0]],
            "name": bearing_name,
            "color": [0.72, 0.74, 0.77],
            "visible": True,
            "mount_frame": axis_frame,
        })

    # 13. End cap
    end_cap_pos, _ = _placement_from_frame(
        kernel, "input_shaft_axis", uv=(0, 0), normal_offset=2.0,
        rotation=(90, (0, 1, 0)),
    )
    parts.append({
        "path": paths["end_cap"],
        "position": end_cap_pos,
        "rotation": [90, [0, 1, 0]],
        "name": "input_end_cap",
        "color": [0.58, 0.61, 0.66],
        "visible": True,
        "mount_frame": "input_shaft_axis",
    })

    k = MechKernel()
    # 在新 kernel 上重建 frame registry 以做 mount_frame 验证
    for frame_name in ["world", "housing_mount_plane", "input_shaft_axis",
                        "intermediate_shaft_axis", "output_shaft_axis"]:
        src = kernel._frame_registry.get(frame_name)
        k.create_reference_plane(
            frame_name,
            origin=src.origin, normal=src.normal, x_axis=src.x_axis,
            parent=src.parent, metadata=dict(src.metadata),
        )
    result = k.assemble(parts, name="two_stage_gearbox")
    if not result.success:
        raise RuntimeError(f"assembly failed: {result.error}")
    return k


# ============================================================
# 5) 渲染与报告
# ============================================================

def _save_section_without_instance_scene(kernel: MechKernel, name: str) -> None:
    """渲染截面（不受 instance 显隐影响）"""
    saved_scene = getattr(kernel, "_assembly_instances", None)
    try:
        kernel._assembly_instances = {}
        result = kernel.render(
            intent="section", section={"axis": "Y", "offset": 0},
            views=["iso", "front", "side"], size=640, annotate=True, quality="evidence",
        )
        _save_render(result, name)
    finally:
        kernel._assembly_instances = saved_scene


def main() -> None:
    print("=" * 72)
    print("Demo 14 v2.7: Reference-Frame-Driven Two-Stage Gearbox")
    print("=" * 72)

    geom = compute_geometry()
    print(f"\n[parameters]")
    print(f"  module = {GEAR_PARAMS['module']} mm")
    print(f"  center_distance stage1 (input↔intermediate): {geom['center_distance_stage1']:.2f} mm")
    print(f"  center_distance stage2 (intermediate↔output): {geom['center_distance_stage2']:.2f} mm")
    print(f"  pitch_diameter input: {2*geom['r_pitch_input']:.1f}, intermediate_large: {2*geom['r_pitch_intermediate_large']:.1f}")
    print(f"  pitch_diameter intermediate_small: {2*geom['r_pitch_intermediate_small']:.1f}, output: {2*geom['r_pitch_output']:.1f}")

    # ---------- 1. 建零件（独立 kernel） ----------
    components = [
        ("housing", lambda: build_housing(geom)),
        ("cover", lambda: build_cover(geom)),
        ("input_shaft", lambda: build_shaft(geom["shaft_length"], geom["shaft_input_d"], "input_shaft")),
        ("intermediate_shaft", lambda: build_shaft(geom["shaft_length"], geom["shaft_intermediate_d"], "intermediate_shaft")),
        ("output_shaft", lambda: build_shaft(geom["shaft_length"], geom["shaft_output_d"], "output_shaft")),
        ("gear_input", lambda: build_gear(GEAR_PARAMS["module"], GEAR_PARAMS["z_input"],
                                          geom["face_width"], geom["bore_input"], "gear_input")),
        ("gear_intermediate_large", lambda: build_gear(GEAR_PARAMS["module"], GEAR_PARAMS["z_intermediate_large"],
                                                       geom["face_width"], geom["bore_intermediate"], "gear_intermediate_large")),
        ("gear_intermediate_small", lambda: build_gear(GEAR_PARAMS["module"], GEAR_PARAMS["z_intermediate_small"],
                                                       geom["face_width"], geom["bore_intermediate"], "gear_intermediate_small")),
        ("gear_output", lambda: build_gear(GEAR_PARAMS["module"], GEAR_PARAMS["z_output"],
                                           geom["face_width"], geom["bore_output"], "gear_output")),
        ("bearing", lambda: build_bearing(geom, "bearing")),
        ("end_cap", lambda: build_end_cap(geom)),
    ]
    paths: dict[str, str] = {}
    reports = []
    for name, builder in components:
        print(f"\n[part] {name}")
        kk = builder()
        reports.append(_report(name, kk))
        paths[name] = _export(kk, name)

    # ---------- 2. 设置装配 reference frames ----------
    print(f"\n[assembly frames]")
    frame_kernel = MechKernel()
    layout = setup_reference_frames(frame_kernel, geom)
    fr = frame_kernel.query_reference()
    for f in fr.value["frames"]:
        print(f"  {f['name']:30s} origin={f['origin']} normal={f['normal']}")

    # ---------- 3. 装配 + mount_frame 关联 ----------
    print("\n[assembly] two-stage gearbox")
    assembly = build_assembly(frame_kernel, paths, geom, layout)
    reports.append(_report("two_stage_gearbox", assembly))
    assembly_step = _export(assembly, "two_stage_gearbox")

    # ---------- 4. validate_assembly: 共轴 + 齿轮啮合 ----------
    print(f"\n[validate_assembly]")
    relations = [
        # 共轴: input shaft axis ↔ input gear / input bearings
        {"kind": "coaxial", "source": "input_shaft_axis", "target": "input_shaft"},
        {"kind": "coaxial", "source": "intermediate_shaft_axis", "target": "intermediate_shaft"},
        {"kind": "coaxial", "source": "output_shaft_axis", "target": "output_shaft"},
        # 齿轮啮合: input ↔ intermediate_large
        {"kind": "gear_mesh", "source": "input_shaft_axis", "target": "intermediate_shaft_axis",
         "parameters": {
             "source_pitch_diameter": 2 * geom["r_pitch_input"],
             "target_pitch_diameter": 2 * geom["r_pitch_intermediate_large"],
             "tolerance": 1.0,
         }},
        # 齿轮啮合: intermediate_small ↔ output
        {"kind": "gear_mesh", "source": "intermediate_shaft_axis", "target": "output_shaft_axis",
         "parameters": {
             "source_pitch_diameter": 2 * geom["r_pitch_intermediate_small"],
             "target_pitch_diameter": 2 * geom["r_pitch_output"],
             "tolerance": 1.0,
         }},
    ]
    va = assembly.validate_assembly(level="standard", relations=relations)
    print(f"  level = {va.value['level']}")
    print(f"  checked = {va.value['checked']} relations")
    print(f"  ok = {va.value['ok']}  issues = {va.value['issue_count']}")
    for iss in va.value["issues"][:5]:
        print(f"    - [{iss['code']}] {iss['message']}")

    # ---------- 5. 渲染证据 ----------
    print(f"\n[render] full evidence")
    full = assembly.render(
        views=["iso", "front", "top", "side"], size=640,
        annotate=True, quality="evidence", show_edges=True,
    )
    _save_render(full, "gearbox_full_evidence")
    _save_views(full, "gearbox_full")

    # 隐壳
    for inst in assembly.query_assembly().value["instances"]:
        if inst["name"] in ("housing", "cover"):
            assembly.set_instance_visibility(inst["id"], False)
    interior = assembly.render(
        views=["iso", "front", "top", "side"], size=640,
        annotate=True, quality="evidence",
    )
    _save_render(interior, "gearbox_interior_evidence")
    _save_views(interior, "gearbox_interior")

    _save_section_without_instance_scene(assembly, "gearbox_section_evidence")

    # 恢复 + presentation
    for inst in assembly.query_assembly().value["instances"]:
        if inst["name"] in ("housing", "cover"):
            assembly.set_instance_visibility(inst["id"], True)
    presentation = assembly.render(
        views=["iso"], size=1280, annotate=True, quality="presentation", show_edges=True,
    )
    _save_render(presentation, "gearbox_presentation")

    turntable = assembly.render(turntable=True, size=480, annotate=True, quality="evidence")
    _save_render(turntable, "gearbox_turntable")

    # ---------- 6. 报告 ----------
    query = assembly.query_assembly()
    manifest = {
        "v": "2.7",
        "parameters": GEAR_PARAMS,
        "derived_geometry": geom,
        "layout": layout,
        "frames": [f for f in fr.value["frames"]],
        "parts": reports,
        "assembly_step": assembly_step,
        "validation": va.value,
        "scene": query.value,
    }
    (OUT / "gearbox_report.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport: {OUT / 'gearbox_report.json'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
