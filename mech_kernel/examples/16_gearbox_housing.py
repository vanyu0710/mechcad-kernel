"""
Demo 15: 单级减速器箱座（下箱体）— MechKernel 复杂零件全流程（v2.11 公开 API）

一个零件覆盖最多类别的公开操作（不碰 experimental，不直接写 build123d）：

1. 箱体毛坯   : rect 160x100 → extrude 72
2. 底板叠加   : rect 200x130 → extrude add 12（z 0..12 实心底板）
3. 内腔切削   : 偏置工作平面 XY offset=6 + rect 148x88 → extrude cut 66
               → 底板厚 6、壁厚 6、开顶内腔 148x88 z6..72、凸缘一次成型
4. 选边倒圆   : select(edge/line) 取底板 4 条外竖棱 → fillet R10
5. 轴承凸台x2 : 自定义工作平面(normal ±Y) + circle Ø60 → extrude 12
6. 轴承孔x2   : hole(direction="y+"/"y-") Ø36 深 18（穿凸台12+壁6）
7. 底脚螺栓x4 : hole(direction="top") Ø13 通孔（穿底板+凸缘 12mm）
8. 排油孔     : hole(direction="bottom") Ø12 穿 6mm 底板
9. 油标凸台   : 自定义工作平面(normal +X) + circle Ø28 → extrude 4
10. 油标孔    : hole(direction="x+") Ø20 深 21
11. 顶缘倒角  : select(edge/line) 取箱壁顶缘(分箱面 z=72)外圈 4 棱 → chamfer 1.5

解析体积 = 407035.5 + 5892·π ≈ 425546 mm³，包围盒 200×130×72。
每步打印 期望/实际 体积增量，末尾 validate + 多视图/剖视/转台渲染 + STEP + JSON 报告。

注: 本内核 shell 为向外偏置语义（且平面底面零厚），不适合向内 hollow，
    故内腔用 偏置工作平面 + extrude cut 实现；嵌套双矩形不成环（内框被忽略），
    底板凸缘用 实心板 + 内腔切削 一次成型。
    凸台外缘圆被柱面 seam 分成 2 段弧，弧端落在 seam 上，ChFi3d 倒角报
    "only 2 faces"，故倒角改为分箱面顶缘外圈平面棱边（几何更规整）。
"""
from __future__ import annotations
import base64
import json
import math
import os
import sys
from pathlib import Path

# Make direct execution work from any current directory:
# python mech_kernel/examples/16_gearbox_housing.py
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
OUT = HERE / "gearbox_housing_out"
OUT.mkdir(exist_ok=True)

# ---------------- 参数（mm） ----------------
BOX = {"length": 160.0, "width": 100.0, "height": 72.0, "wall": 6.0}
FOOT = {"length": 200.0, "width": 130.0, "height": 12.0}
BOSS = {"diameter": 56.0, "protrusion": 12.0, "center_z": 42.0}  # Ø56: z14..70 与顶面/凸缘脱开（避免相切几何）
BORE = {"diameter": 36.0, "depth": 18.0}
BOLT = {"diameter": 13.0, "depth": 70.0, "offset_x": 88.0, "offset_y": 54.0}
DRAIN = {"diameter": 12.0, "depth": 6.0, "x": 60.0}
SIGHT = {"boss_diameter": 28.0, "boss_protrusion": 4.0, "center_z": 30.0,
         "hole_diameter": 20.0, "hole_depth": 21.0}
FILLET_R = 10.0
CHAMFER_LEN = 1.5

# ---------------- 解析体积基准（与参数联动推导） ----------------
BOX_XY = BOX["length"] * BOX["width"]
CAV_XY = (BOX["length"] - 2 * BOX["wall"]) * (BOX["width"] - 2 * BOX["wall"])
CAV_H = BOX["height"] - BOX["wall"]
FOOT_XY = FOOT["length"] * FOOT["width"]

V_BOX = BOX_XY * BOX["height"]                                        # 1,152,000
V_SLAB = (FOOT_XY - BOX_XY) * FOOT["height"]                          # +120,000
V_CAVITY = -CAV_XY * CAV_H                                            # -859,584
V_FILLET = -4 * (1 - math.pi / 4) * FILLET_R ** 2 * FOOT["height"]    # -4800+1200π
V_BOSSES = 2 * math.pi * (BOSS["diameter"] / 2) ** 2 * BOSS["protrusion"]
V_BORES = -2 * math.pi * (BORE["diameter"] / 2) ** 2 * BORE["depth"]
V_BOLTS = -4 * math.pi * (BOLT["diameter"] / 2) ** 2 * FOOT["height"]
V_DRAIN = -math.pi * (DRAIN["diameter"] / 2) ** 2 * DRAIN["depth"]
V_SIGHT_BOSS = math.pi * (SIGHT["boss_diameter"] / 2) ** 2 * SIGHT["boss_protrusion"]
V_SIGHT_HOLE = -math.pi * (SIGHT["hole_diameter"] / 2) ** 2 * (
    BOX["wall"] + SIGHT["boss_protrusion"])  # 刀具 x74..100，实切 壁6+凸台4 = 10mm
_R = BOSS["diameter"] / 2
_c = CHAMFER_LEN
# 分箱面顶缘（z=72，外圈矩形 BOX 长×宽）倒角 c：
# 直段 ½c²·(ΣL−8c)，4 个凸角按两棱倒角体之并 = 2c³/3（OCC 实测，非斜切 c³/3）
_PERIM = 2 * (BOX["length"] + BOX["width"])
V_CHAMFER = -(0.5 * _c ** 2 * (_PERIM - 8 * _c) + 4 * (2 * _c ** 3 / 3))  # = -580.5

# stepwise 期望体积（逐特征累加，用于 demo 每步核对）
EXPECTED_AFTER = []


def _expected_series():
    s = V_BOX
    EXPECTED_AFTER.append(("箱体毛坯", s))
    s += V_SLAB
    EXPECTED_AFTER.append(("底板叠加", s))
    s += V_CAVITY
    EXPECTED_AFTER.append(("内腔切削", s))
    s += V_FILLET
    EXPECTED_AFTER.append(("选边倒圆R10", s))
    s += V_BOSSES
    EXPECTED_AFTER.append(("轴承凸台x2", s))
    s += V_BORES
    EXPECTED_AFTER.append(("轴承孔x2", s))
    s += V_BOLTS
    EXPECTED_AFTER.append(("底脚螺栓x4", s))
    s += V_DRAIN
    EXPECTED_AFTER.append(("排油孔", s))
    s += V_SIGHT_BOSS
    EXPECTED_AFTER.append(("油标凸台", s))
    s += V_SIGHT_HOLE
    EXPECTED_AFTER.append(("油标孔", s))
    s += V_CHAMFER
    EXPECTED_AFTER.append(("凸台倒角", s))
    return s


ANALYTIC_TOTAL = _expected_series()


def volume_of(kernel):
    return kernel.query("_current_geometry", "volume").value


def check_step(kernel, label, step_idx, tol=2.0):
    """打印该步后 实际体积 vs 解析期望。"""
    expected = EXPECTED_AFTER[step_idx][1]
    actual = volume_of(kernel)
    ok = abs(actual - expected) <= tol
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label:<10s} 体积 期望={expected:,.1f} 实际={actual:,.1f} "
          f"Δ={actual - expected:+.3f} mm³")
    return actual


# ---------- 渲染/导出助手（与 demo 13 同款） ----------
def render_part(kernel, name, size=(520, 400)):
    from mech_kernel.renderer import Renderer
    from PIL import Image
    import io
    r = Renderer(image_size=size)
    views = r.render(kernel._current_geometry, "iso_only",
                     geometry_revision=kernel._geometry_revision)
    png = views.get("iso") or views.get("default")
    if not png:
        print(f"  [WARN] {name} 渲染为空")
        return None
    path = OUT / f"{name}.png"
    Image.open(io.BytesIO(png)).save(path)
    print(f"  渲染: {path}")
    return path


def save_render(result, name):
    """保存 render op 返回的拼图 PNG。"""
    if not result.render_base64:
        print(f"  渲染为空: {name}")
        return None
    path = OUT / f"{name}.png"
    path.write_bytes(base64.b64decode(result.render_base64))
    print(f"  多视图: {path}")
    return path


def export_step(kernel, name):
    path = OUT / f"{name}.step"
    r = kernel.export(str(path), format="step")
    if r.success:
        print(f"  导出: {path} ({path.stat().st_size} bytes)")
    else:
        print(f"  导出失败: {r.error}")
    return str(path)


# ---------- 建模主流程 ----------
def build_housing(verbose=True):
    k = MechKernel()
    step = 0

    # 1. 箱体毛坯
    k.create_workplane("base", "XY")
    k.new_sketch("base", "body_blank")
    k.add_rectangle("body_blank", BOX["length"], BOX["width"])
    k.close_sketch("body_blank")
    k.extrude("body_blank", depth=BOX["height"], mode="new_body", name="body_blank")
    if verbose:
        check_step(k, "箱体毛坯", step); step += 1

    # 2. 底板叠加（z 0..12 实心，200x130）
    k.new_sketch("base", "base_plate")
    k.add_rectangle("base_plate", FOOT["length"], FOOT["width"])
    k.close_sketch("base_plate")
    k.extrude("base_plate", depth=FOOT["height"], mode="add", name="base_plate")
    if verbose:
        check_step(k, "底板叠加", step); step += 1

    # 3. 内腔切削（偏置工作平面 z=6 上 148x88 向上切 66：
    #    底板厚 6、壁厚 6、开顶内腔、底板凸缘一次成型）
    k.create_workplane("wp_cavity", "XY", offset=BOX["wall"])
    k.new_sketch("wp_cavity", "cavity")
    k.add_rectangle("cavity", BOX["length"] - 2 * BOX["wall"],
                    BOX["width"] - 2 * BOX["wall"])
    k.close_sketch("cavity")
    k.extrude("cavity", depth=CAV_H, mode="cut", name="cavity_cut")
    if verbose:
        check_step(k, "内腔切削", step); step += 1

    # 4. 选边倒圆：select 线边 → 4 条外竖棱（center 在凸缘四角）
    sel = k.select(filter_type="line", element_type="edge")
    corner_refs = [
        it["ref"] for it in sel.value["selected"]
        if abs(abs(it["center"][0]) - FOOT["length"] / 2) < 0.5
        and abs(abs(it["center"][1]) - FOOT["width"] / 2) < 0.5
        and abs(it["center"][2] - FOOT["height"] / 2) < 0.5
    ]
    assert len(corner_refs) == 4, f"底脚竖棱筛选异常: {corner_refs}"
    k.fillet(FILLET_R, edges=corner_refs, name="foot_corner_fillet")
    if verbose:
        check_step(k, "选边倒圆", step); step += 1

    # 5. 轴承凸台×2（自定义工作平面，法向 ±Y，沿法向拉伸）
    for side, sign in (("ypos", 1.0), ("yneg", -1.0)):
        wp = f"wp_boss_{side}"
        sk = f"boss_{side}"
        k.create_workplane(wp, "custom",
                           origin=(0.0, sign * BOX["width"] / 2, BOSS["center_z"]),
                           normal=(0.0, sign, 0.0))
        k.new_sketch(wp, sk)
        k.add_circle(sk, (0, 0), BOSS["diameter"] / 2)
        k.close_sketch(sk)
        k.extrude(sk, depth=BOSS["protrusion"], mode="add", name=f"bearing_boss_{side}")
    if verbose:
        check_step(k, "轴承凸台", step); step += 1

    # 6. 轴承孔×2（从凸台端面进孔：穿凸台12 + 壁6，止于内腔壁面）
    k.hole(position=(0.0, BOSS["center_z"]), diameter=BORE["diameter"],
           depth=BORE["depth"], direction="y+", name="bearing_bore_ypos")
    k.hole(position=(0.0, BOSS["center_z"]), diameter=BORE["diameter"],
           depth=BORE["depth"], direction="y-", name="bearing_bore_yneg")
    if verbose:
        check_step(k, "轴承孔", step); step += 1

    # 7. 底脚螺栓孔×4（Ø13 通孔，孔位完全落在凸缘内、避开箱壁）
    for sx in (1, -1):
        for sy in (1, -1):
            k.hole(position=(sx * BOLT["offset_x"], sy * BOLT["offset_y"]),
                   diameter=BOLT["diameter"], depth=BOLT["depth"],
                   direction="top", name=f"bolt_hole_{'p' if sx > 0 else 'n'}{'x'}{'p' if sy > 0 else 'n'}y")
    if verbose:
        check_step(k, "底脚螺栓", step); step += 1

    # 8. 排油孔（底面进孔，穿 6mm 底板）
    k.hole(position=(DRAIN["x"], 0.0), diameter=DRAIN["diameter"],
           depth=DRAIN["depth"], direction="bottom", name="drain_hole")
    if verbose:
        check_step(k, "排油孔", step); step += 1

    # 9. 油标凸台（自定义工作平面，法向 +X）
    k.create_workplane("wp_sight", "custom",
                       origin=(BOX["length"] / 2, 0.0, SIGHT["center_z"]),
                       normal=(1.0, 0.0, 0.0))
    k.new_sketch("wp_sight", "sight_boss")
    k.add_circle("sight_boss", (0, 0), SIGHT["boss_diameter"] / 2)
    k.close_sketch("sight_boss")
    k.extrude("sight_boss", depth=SIGHT["boss_protrusion"], mode="add", name="sight_boss")
    if verbose:
        check_step(k, "油标凸台", step); step += 1

    # 10. 油标孔（从包围盒 +X 面进孔：凸台4 + 壁6 = 实切 10mm）
    k.hole(position=(0.0, SIGHT["center_z"]), diameter=SIGHT["hole_diameter"],
           depth=SIGHT["hole_depth"], direction="x+", name="sight_hole")
    if verbose:
        check_step(k, "油标孔", step); step += 1

    # 11. 顶缘倒角：分箱面（z=72）外圈 4 条平面棱边，断边倒角 1.5
    sel = k.select(filter_type="line", element_type="edge")
    rim_refs = [
        it["ref"] for it in sel.value["selected"]
        if abs(it["center"][2] - BOX["height"]) < 0.5
        and (
            (abs(abs(it["center"][0]) - BOX["length"] / 2) < 0.5 and abs(it["center"][1]) < 0.5)
            or (abs(abs(it["center"][1]) - BOX["width"] / 2) < 0.5 and abs(it["center"][0]) < 0.5)
        )
    ]
    assert len(rim_refs) == 4, f"顶缘外圈棱边筛选异常: {rim_refs}"
    k.chamfer(CHAMFER_LEN, edges=rim_refs, name="parting_rim_chamfer")
    if verbose:
        check_step(k, "凸台倒角", step); step += 1

    return k


def main():
    print("=" * 66)
    print("Demo 16: 单级减速器箱座 — MechKernel 复杂零件全流程（v2.11 公开 API）")
    print("=" * 66)
    print(f"解析总体积 = {ANALYTIC_TOTAL:,.1f} mm³  "
          f"(= 407035.5 + 5892π)，包围盒 200×130×72\n")

    print("[1/3] 建模（11 步特征，每步核对解析体积）")
    k = build_housing()

    print("\n[2/3] 校验 + 渲染证据")
    vr = k.validate_geometry(level="standard")
    print(f"  validate_geometry: success={vr.success} "
          f"value={json.dumps(vr.value.get('summary', vr.value), ensure_ascii=False)[:200]}")
    bb = k.query("_current_geometry", "bounding_box").value
    print(f"  包围盒: {bb['size_x']:.0f} x {bb['size_y']:.0f} x {bb['size_z']:.0f} mm  "
          f"(期望 200 x 130 x 72)")
    fc = k.query("_current_geometry", "face_count").value
    print(f"  面数: {fc}  特征数: {len(k.feature_graph.nodes)}")

    final_vol = volume_of(k)
    print(f"  最终体积: {final_vol:,.1f} mm³ (解析 {ANALYTIC_TOTAL:,.1f}, "
          f"相对误差 {abs(final_vol - ANALYTIC_TOTAL) / ANALYTIC_TOTAL:.2e})")

    render_part(k, "housing_iso")
    save_render(k.render(views=["iso", "front", "top", "side"], size=640,
                         annotate=True, quality="presentation"), "housing_views")
    save_render(k.render(intent="section", section={"axis": "X", "offset": 0},
                         size=640, annotate=True), "housing_section_x")
    save_render(k.render(intent="section", section={"axis": "Y", "offset": 0},
                         size=640, annotate=True), "housing_section_y")
    save_render(k.render(turntable=True, size=480, annotate=True), "housing_turntable")

    print("\n[3/3] 导出 STEP + JSON 报告")
    export_step(k, "gearbox_housing")

    ops = [{"op": e.get("op"), "name": e.get("name", ""),
            "feature_id": e.get("feature_id")} for e in k._op_history]
    report = {
        "part": "单级减速器箱座 gearbox housing (lower)",
        "kernel_ops": len(ops),
        "features": len(k.feature_graph.nodes),
        "volume_mm3": final_vol,
        "volume_analytic_mm3": ANALYTIC_TOTAL,
        "volume_rel_err": abs(final_vol - ANALYTIC_TOTAL) / ANALYTIC_TOTAL,
        "bbox_mm": {"x": bb["size_x"], "y": bb["size_y"], "z": bb["size_z"]},
        "params": {"BOX": BOX, "FOOT": FOOT, "BOSS": BOSS, "BORE": BORE,
                   "BOLT": BOLT, "DRAIN": DRAIN, "SIGHT": SIGHT,
                   "FILLET_R": FILLET_R, "CHAMFER_LEN": CHAMFER_LEN},
        "operations": ops,
    }
    report_path = OUT / "gearbox_housing_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"  报告: {report_path}")

    print("\n" + "=" * 66)
    print("产出目录: " + str(OUT))
    print("=" * 66)


if __name__ == "__main__":
    main()
