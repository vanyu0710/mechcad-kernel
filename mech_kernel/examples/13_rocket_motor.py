"""
Demo 13: 业余探空火箭固体发动机（壳体 + 堵头 + CD 喷口 + 整机装配）
— MechKernel 生产力实测（v2.2：revolve 线剖面 + assemble + 多视图截面）

1. 壳体  : Ø60 外径 × 3mm 壁厚 × 300mm 管（同心圆 → 拉伸成环）
2. 堵头  : Ø60 圆盘 × 15mm + 中心点火孔 Ø8 + 外缘倒角
3. 喷口  : 收敛-扩张(CD)喷口 —— line 闭合半剖面绕 Y 轴 revolve
4. 装配  : 三件按坐标 assemble → 整机 STEP
"""
from __future__ import annotations
import os, sys, math
from pathlib import Path

from mech_kernel import MechKernel

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).parent
OUT = HERE / "rocket_motor_out"
OUT.mkdir(exist_ok=True)


def render_part(kernel, name, size=(520, 400)):
    from mech_kernel.renderer import Renderer
    from PIL import Image
    import io
    r = Renderer(image_size=size)
    views = r.render(kernel._current_geometry, "iso_only", geometry_revision=kernel._geometry_revision)
    png = views.get("iso") or views.get("default")
    if not png:
        print(f"  [WARN] {name} 渲染为空")
        return None
    path = OUT / f"{name}.png"
    Image.open(io.BytesIO(png)).save(path)
    print(f"  渲染: {path}")
    return path


def part_report(name, kernel, note=""):
    vol = kernel.query("_current_geometry", "volume").value
    bb = kernel.query("_current_geometry", "bounding_box").value
    feats = len(kernel.feature_graph.nodes)
    print(f"  [{name}] 体积={vol:,.0f} mm3  包围盒=({bb['size_x']:.0f} x {bb['size_y']:.0f} x {bb['size_z']:.0f})  特征数={feats} {note}")
    return {"name": name, "volume": vol, "bbox": bb}


def export_step(kernel, name):
    path = OUT / f"{name}.step"
    r = kernel.export(str(path), format="step")
    if r.success:
        print(f"  导出: {path} ({path.stat().st_size} bytes)")
    else:
        print(f"  导出失败: {r.error}")
    return str(path)


def save_render(result, name):
    """保存 render op 返回的拼图 PNG。"""
    import base64
    if not result.render_base64:
        print(f"  渲染为空: {name}")
        return None
    path = OUT / f"{name}.png"
    path.write_bytes(base64.b64decode(result.render_base64))
    print(f"  多视图: {path}")
    return path


# ---------- 1. 壳体 ----------
def build_case():
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "case_ring")
    k.add_circle("case_ring", center=(0, 0), radius=30)   # 外径 Ø60
    k.add_circle("case_ring", center=(0, 0), radius=27)   # 内径 Ø54（壁厚 3）
    k.close_sketch("case_ring")
    k.extrude("case_ring", depth=300, mode="new_body", name="case")
    return k


# ---------- 2. 堵头 ----------
def build_closure():
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "closure_disc")
    k.add_circle("closure_disc", center=(0, 0), radius=30)
    k.close_sketch("closure_disc")
    k.extrude("closure_disc", depth=15, mode="new_body", name="closure")
    k.chamfer(2, edges="all")          # 外缘倒角
    k.hole(position=(0, 0), diameter=8)  # 点火孔
    return k


# ---------- 3. CD 喷口（line 闭合半剖面 revolve）----------
def build_cd_nozzle():
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "cd_profile")
    # 半剖面（绕 Y 轴）：轴心(0,0)→入口唇(22,0)→收敛(8,20)→喉部(8,30)→扩张(16,50)→轴心(0,50)→闭合
    for seg in [((0, 0), (22, 0)), ((22, 0), (8, 20)), ((8, 20), (8, 30)),
                ((8, 30), (16, 50)), ((16, 50), (0, 50)), ((0, 50), (0, 0))]:
        k.add_line("cd_profile", start=seg[0], end=seg[1])
    k.close_sketch("cd_profile")
    k.revolve("cd_profile", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body", name="cd_nozzle")
    return k


# ---------- 4. 装配 ----------
def build_assembly(case_step, closure_step, nozzle_step):
    k = MechKernel()
    k.assemble([
        {"path": case_step,    "position": [0, 0, 0]},                    # 壳体 z=0..300
        {"path": closure_step, "position": [0, 0, 300]},                  # 堵头 压在前端 z=300..315
        {"path": nozzle_step,  "position": [0, 0, -50], "rotation": [90, [1, 0, 0]]},  # CD 喷口绕X转90°使轴对齐Z
    ], name="motor_assembly")
    return k


def main():
    print("=" * 66)
    print("Demo 13: 业余探空火箭固体发动机 — MechKernel 生产力实测（v2.2）")
    print("=" * 66)

    print("\n[1/4] 壳体（Ø60 x 壁厚3 x 长300 管）")
    case = build_case()
    part_report("壳体", case)
    render_part(case, "case")
    case_step = export_step(case, "case")

    print("\n[2/4] 堵头（Ø60 x 15 盘 + 点火孔Ø8 + 倒角）")
    closure = build_closure()
    part_report("堵头", closure)
    render_part(closure, "closure")
    closure_step = export_step(closure, "closure")

    print("\n[3/4] CD 喷口（收敛-喉部-扩张，line 剖面 revolve）")
    nozzle = build_cd_nozzle()
    part_report("喷口", nozzle)
    render_part(nozzle, "nozzle_cd")
    nozzle_step = export_step(nozzle, "nozzle_cd")

    print("\n[4/4] 整机装配（壳体 + 堵头 + 喷口）")
    asm = build_assembly(case_step, closure_step, nozzle_step)
    part_report("发动机整机", asm, note="（体积=三件之和）")
    render_part(asm, "motor_assembly")
    save_render(asm.render(views=["iso", "front", "top", "side"], size=640, annotate=True), "motor_views")
    save_render(asm.render(turntable=True, size=480, annotate=True), "motor_turntable")
    # 发动机轴向为 Z；沿 X 保留一半，更适合观察壳体内腔、喷口喉部和堵头。
    save_render(asm.render(section={"axis": "X", "offset": 0}, size=640, annotate=True), "motor_section")
    export_step(asm, "motor_assembly")

    print("\n" + "=" * 66)
    print("产出目录: " + str(OUT))
    print("=" * 66)


if __name__ == "__main__":
    main()
