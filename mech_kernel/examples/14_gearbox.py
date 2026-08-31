"""
Demo 14 v3: In-kernel 2-stage gearbox (reverted compound layout)

经典 2 段齿轮减速器 (reverted compound inline):
- 1 根 input/output shaft (蓝, 贯穿 housing)
- 1 根 intermediate shaft (蓝, 偏 Y=80)
- 4 齿轮 in-kernel (build_involute_gear)
- housing 包裹

中心距: 80 mm (统一, z=[20, 60, 60, 20])
减速比: 3:1 (input → intermediate → output)

视觉: 真正 SolidWorks 风格, 全部用 build123d 在 kernel 内构造 + MechKernel 渲染
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from mech_kernel import MechKernel

HERE = Path(__file__).parent
OUT = HERE / "gearbox_out"
OUT.mkdir(exist_ok=True)


# ============================================================
# 1) 全局参数
# ============================================================

GEAR_PARAMS = {
    "module": 2.0,
    "z_input": 20,
    "z_intermediate": 60,   # 两段都用
    "z_output": 20,
    "face_width": 18.0,
    "shaft_input_d": 16.0,
    "shaft_intermediate_d": 22.0,
    "shaft_output_d": 20.0,
}

CENTER_DISTANCE = GEAR_PARAMS["module"] * (GEAR_PARAMS["z_input"] + GEAR_PARAMS["z_intermediate"]) / 2  # 80
REDUCTION_RATIO = GEAR_PARAMS["z_intermediate"] // GEAR_PARAMS["z_input"]  # 3

SHAFT_LENGTH = 200.0
SHAFT_Z = 30.0          # 轴中心 Z 高度
HOUSING = {"length": 220.0, "width": 200.0, "height": 100.0, "wall": 6.0}

GEAR_X = {
    "input": -50.0,
    "intermediate_large": -50.0,
    "intermediate_small": +50.0,
    "output": +50.0,
}

INPUT_RPM = 1500
INTERMEDIATE_RPM = INPUT_RPM // REDUCTION_RATIO  # 500
OUTPUT_RPM = INTERMEDIATE_RPM // REDUCTION_RATIO  # 166


def main():
    print("=" * 72)
    print("Demo 14 v3: In-kernel 2-Stage Gearbox (build123d + MechKernel render)")
    print("=" * 72)

    print(f"\n[parameters]")
    print(f"  module={GEAR_PARAMS['module']}, z=[{GEAR_PARAMS['z_input']}, "
          f"{GEAR_PARAMS['z_intermediate']}, {GEAR_PARAMS['z_intermediate']}, "
          f"{GEAR_PARAMS['z_output']}]")
    print(f"  center_distance = {CENTER_DISTANCE} mm")
    print(f"  reduction = {REDUCTION_RATIO}:1")
    print(f"  RPM: input {INPUT_RPM} → intermediate {INTERMEDIATE_RPM} → output {OUTPUT_RPM}")

    # ---------- 1. 全部用 build123d 在 kernel 内构造 ----------
    print(f"\n[build in kernel]")
    from build123d import (
        BuildPart, BuildSketch, Plane, add, extrude,
        Part, Location, Box, Circle,
    )
    from build123d import Axis as BdAxis
    from mech_kernel.gear import build_involute_gear
    from build123d.exporters3d import export_step

    def make_shaft(d: float, length: float = SHAFT_LENGTH) -> Part:
        """圆柱, 沿 Z, 中心在原点"""
        with BuildPart(Plane.XY) as bp:
            with BuildSketch() as s:
                add(Circle(d / 2))
            extrude(amount=length / 2, both=True)  # 居中
        return bp.part

    def make_housing_box() -> Part:
        """中空 housing: 1 大 box - 1 小 box (内部空腔, 形成 4 壁).

        顶部和底部都打通 (内 box 高度 = 外 box 高度), 所以从 ISO 视角能看到内部齿轮.
        """
        from build123d import Mode, Align
        L = HOUSING["length"]
        W = HOUSING["width"]
        H = HOUSING["height"]
        wall = HOUSING["wall"]
        with BuildPart(Plane.XY) as bp:
            # 外 box (中心)
            add(Box(L, W, H, align=(Align.CENTER, Align.CENTER, Align.CENTER)))
            # 内 box (空腔, 上下打通, 4 壁 = wall 厚)
            add(Box(L - 2 * wall, W - 2 * wall, H,
                    align=(Align.CENTER, Align.CENTER, Align.CENTER)),
                mode=Mode.SUBTRACT)
        return bp.part

    def place_part_z_to_x(part: Part, x: float, y: float, z: float) -> Part:
        """part 沿 Z, 旋转成沿 X, 移动到 (x, y, z)"""
        rotated = part.rotate(BdAxis((0, 0, 0), (0, 1, 0)), 90)
        return rotated.moved(Location((x, y, z)))

    # Shafts
    io_shaft = make_shaft(GEAR_PARAMS["shaft_input_d"])
    output_shaft = make_shaft(GEAR_PARAMS["shaft_output_d"])
    inter_shaft = make_shaft(GEAR_PARAMS["shaft_intermediate_d"])

    # Gears (默认沿 Z, 总长 = face_width)
    g_input = build_involute_gear(module=GEAR_PARAMS["module"], teeth=GEAR_PARAMS["z_input"],
                                width=GEAR_PARAMS["face_width"], bore=GEAR_PARAMS["shaft_input_d"])
    g_inter = build_involute_gear(module=GEAR_PARAMS["module"], teeth=GEAR_PARAMS["z_intermediate"],
                                 width=GEAR_PARAMS["face_width"], bore=GEAR_PARAMS["shaft_intermediate_d"])
    g_output = build_involute_gear(module=GEAR_PARAMS["module"], teeth=GEAR_PARAMS["z_output"],
                                 width=GEAR_PARAMS["face_width"], bore=GEAR_PARAMS["shaft_output_d"])

    # Housing
    housing = make_housing_box()

    # 放置: 旋转到 X, 移动到位置
    io_shaft_x = place_part_z_to_x(io_shaft, 0, 0, SHAFT_Z)
    output_shaft_x = place_part_z_to_x(output_shaft, 0, 0, SHAFT_Z)
    inter_shaft_x = place_part_z_to_x(inter_shaft, 0, CENTER_DISTANCE, SHAFT_Z)

    g_input_p = place_part_z_to_x(g_input, GEAR_X["input"], 0, SHAFT_Z)
    g_inter_large_p = place_part_z_to_x(g_inter, GEAR_X["intermediate_large"], CENTER_DISTANCE, SHAFT_Z)
    g_inter_small_p = place_part_z_to_x(g_inter, GEAR_X["intermediate_small"], CENTER_DISTANCE, SHAFT_Z)
    g_output_p = place_part_z_to_x(g_output, GEAR_X["output"], 0, SHAFT_Z)

    # Fuse 全部
    assembly = housing
    for p in [g_input_p, g_inter_large_p, g_inter_small_p, g_output_p,
              io_shaft_x, inter_shaft_x, output_shaft_x]:
        assembly = assembly + p

    bb = assembly.bounding_box()
    print(f"  assembly bbox = {bb.max.X - bb.min.X:.0f} × {bb.max.Y - bb.min.Y:.0f} × "
          f"{bb.max.Z - bb.min.Z:.0f} mm")
    print(f"  assembly vol = {assembly.volume:.0f} mm³")

    # 验证啮合中心距
    print(f"\n[validate mesh center distance]")
    for n1, n2 in [("input", "intermediate_large"), ("intermediate_small", "output")]:
        if n1 == "input":
            c1 = (GEAR_X["input"], 0, SHAFT_Z)
            c2 = (GEAR_X["intermediate_large"], CENTER_DISTANCE, SHAFT_Z)
        else:
            c1 = (GEAR_X["intermediate_small"], CENTER_DISTANCE, SHAFT_Z)
            c2 = (GEAR_X["output"], 0, SHAFT_Z)
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))
        ok = "✓" if abs(d - CENTER_DISTANCE) < 0.01 else "✗"
        print(f"  {n1} ↔ {n2}: dist={d:.3f}mm (cd={CENTER_DISTANCE}) {ok}")

    # ---------- 2. MechKernel 渲染 ----------
    print(f"\n[render via MechKernel]")

    k = MechKernel()
    k.adaptive_renderer.suspended = True  # 不自动渲染
    k._current_geometry = assembly

    # 4 视角渲染
    r = k.render(
        views=["iso", "front", "top", "side"],
        size=720,
        annotate=True,
        quality="evidence",
        show_edges=True,
    )
    if r.success:
        for view, png in (r.render_views or {}).items():
            if png:
                path = OUT / f"gearbox_v3_{view}.png"
                if isinstance(png, str):
                    import base64
                    path.write_bytes(base64.b64decode(png))
                else:
                    path.write_bytes(png)
                print(f"  saved: {path.name} ({path.stat().st_size} bytes)")

    # 单独 ISO 大图
    r = k.render(views=["iso"], size=1280, annotate=True, quality="presentation", show_edges=True)
    if r.success and r.render_base64:
        import base64
        path = OUT / "gearbox_v3_presentation.png"
        path.write_bytes(base64.b64decode(r.render_base64))
        print(f"  saved: {path.name} ({path.stat().st_size} bytes)")

    # 隐藏 housing 看内部齿轮
    r = k.render(views=["iso", "front"], size=720, annotate=True, quality="evidence", show_edges=True)
    if r.success:
        for view, png in (r.render_views or {}).items():
            if png:
                path = OUT / f"gearbox_v3_{view}_with_housing.png"
                if isinstance(png, str):
                    import base64
                    path.write_bytes(base64.b64decode(png))
                else:
                    path.write_bytes(png)
                print(f"  saved: {path.name} ({path.stat().st_size} bytes)")

    # ---------- 3. STEP 导出 ----------
    step_path = OUT / "two_stage_gearbox_v3.step"
    export_step(assembly, str(step_path))
    print(f"\n  STEP: {step_path.name} ({step_path.stat().st_size} bytes)")

    # ---------- 4. 减速比示意图 ----------
    _render_ratio_diagram(OUT / "gearbox_v3_ratio.png")

    # ---------- 5. 报告 ----------
    import json
    report = {
        "version": "v3",
        "parameters": GEAR_PARAMS,
        "derived": {
            "center_distance": CENTER_DISTANCE,
            "reduction_ratio": f"{REDUCTION_RATIO}:1",
            "input_rpm": INPUT_RPM,
            "intermediate_rpm": INTERMEDIATE_RPM,
            "output_rpm": OUTPUT_RPM,
        },
        "geometry": {
            "shaft_length": SHAFT_LENGTH,
            "shaft_z": SHAFT_Z,
            "housing": HOUSING,
            "gear_x": GEAR_X,
        },
        "validation": {
            "input_to_intermediate_large": CENTER_DISTANCE,
            "intermediate_small_to_output": CENTER_DISTANCE,
        },
    }
    (OUT / "gearbox_v3_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n  report: gearbox_v3_report.json")
    print("=" * 72)


def _render_ratio_diagram(out_path: Path):
    """2D 示意图: 3 根轴 + 4 齿轮 + RPM 标注"""
    fig, ax = plt.subplots(figsize=(14, 6))

    L = 100
    # 3 根轴
    ax.plot([-L, L], [3, 3], 'b-', linewidth=4, label='Input/Output shaft (Ø16/20)')
    ax.plot([-L, L], [2, 2], 'b-', linewidth=4, label='Intermediate shaft (Ø22)')
    # Y=1 不是单独轴, 是 output shaft 第二段 (用更细的线)
    ax.plot([-L, L], [1, 1], 'b--', linewidth=2, alpha=0.5, label='Input/Output shaft')

    # 4 齿轮
    ax.scatter([-50], [3], s=200, c='orange', edgecolors='black', linewidths=1, zorder=5, label='Input gear (z=20)')
    # 2 个中间齿轮 (2 段, 同 z=60)
    ax.scatter([-50], [2], s=600, c='orange', edgecolors='black', linewidths=1, zorder=5)
    ax.scatter([50], [2], s=600, c='orange', edgecolors='black', linewidths=1, zorder=5)
    ax.scatter([50], [1], s=200, c='orange', edgecolors='black', linewidths=1, zorder=5, label='Output gear (z=20)')

    # RPM 标注
    ax.annotate(f'Input\n{INPUT_RPM} RPM',
                xy=(-95, 3), xytext=(-130, 3.2),
                fontsize=12, weight='bold', color='red',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#ffebeb', edgecolor='red'))
    ax.annotate(f'Intermediate\n{INTERMEDIATE_RPM} RPM',
                xy=(-95, 2), xytext=(-130, 2.2),
                fontsize=12, weight='bold', color='darkorange',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#fff3e0', edgecolor='darkorange'))
    ax.annotate(f'Output\n{OUTPUT_RPM} RPM',
                xy=(95, 1), xytext=(120, 1.2),
                fontsize=12, weight='bold', color='green',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#e8f5e9', edgecolor='green'))

    # 中心距
    for x in [-50, 50]:
        y_low = 1 if x < 0 else 2
        y_high = 2 if x < 0 else 3
        ax.annotate('', xy=(x, y_low + 0.2), xytext=(x, y_high - 0.2),
                    arrowprops=dict(arrowstyle='<->', color='black', lw=1.2))
        ax.text(x + 4, (y_low + y_high) / 2, f'cd={CENTER_DISTANCE}', fontsize=9, weight='bold')

    # 减速比
    ax.text(0, 0.3, f'Reduction Ratio: 3 : 1    ({INPUT_RPM} → {INTERMEDIATE_RPM} → {OUTPUT_RPM} RPM)',
            ha='center', fontsize=14, weight='bold',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#e3f2fd', edgecolor='navy', linewidth=2))

    ax.set_xlim(-160, 160)
    ax.set_ylim(0, 4)
    ax.set_xlabel('Shaft position (X, mm)', fontsize=10)
    ax.set_ylabel('Y position (mm)', fontsize=10)
    ax.set_title('2-Stage Gearbox: Shaft / Gear / RPM Diagram', fontsize=13, weight='bold')
    # 单独的中间齿轮 legend entry
    from matplotlib.lines import Line2D
    custom_legend = [
        Line2D([0], [0], color='blue', lw=4, label='Shaft'),
        Line2D([0], [0], marker='o', color='w', markerfacecolor='orange', markersize=12, label='Gear'),
    ]
    ax.legend(handles=custom_legend, loc='lower center', fontsize=10, framealpha=0.9, ncol=2)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  saved: {out_path.name} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
