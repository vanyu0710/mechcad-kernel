"""
Demo 10: Boolean op 显式 API（v1.7 新增）

4 个 boolean demo:
1. union: 两个 box 合并
2. subtract: 盒 - 圆柱（多 tool 一起切）
3. intersect: 盒 ∩ 圆柱
4. 实际工程：L 形盒用 boolean union
"""
from __future__ import annotations
import os, math
from pathlib import Path
HERE = Path(__file__).parent
OUT = HERE / "boolean_out"
OUT.mkdir(exist_ok=True)


def render(k, name, out_path, suptitle=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mech_kernel.renderer import Renderer
    from PIL import Image
    import io
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    if k._current_geometry is None:
        print(f"[WARN] {name} 几何为空")
        return
    r = Renderer()
    views = r.render(k._current_geometry, "full", geometry_revision=k._geometry_revision)
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for i, vn in enumerate(['iso', 'top', 'front']):
        if views.get(vn):
            axes[i].imshow(Image.open(io.BytesIO(views[vn])))
        axes[i].set_title(vn, fontsize=14, weight="bold")
        axes[i].axis("off")
    vol = k._current_geometry.volume
    plt.suptitle(suptitle or f"{name} | vol = {vol:.2f} mm³", fontsize=13, weight="bold")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] {name}: vol = {vol:.2f} mm³")


def demo1_union():
    """1. union: 两个 box 合并"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'a')
    k.add_rectangle('a', 40, 30, center=[0, 0])
    k.close_sketch('a')
    k.new_sketch('XY', 'b')
    k.add_rectangle('b', 30, 20, center=[10, 5])
    k.close_sketch('b')
    k.boolean('a', ['b'], operation='union', name='two_boxes')
    render(k, "demo1_union", OUT / "01_union.png", "Boolean union: 40×30 + 30×20")


def demo2_subtract_multi():
    """2. subtract 多 tool: 盒 - 2 圆柱"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'plate')
    k.add_rectangle('plate', 80, 50, center=[0, 0])
    k.close_sketch('plate')
    k.new_sketch('XY', 'h1')
    k.add_circle('h1', center=[-20, 0], radius=5)
    k.close_sketch('h1')
    k.new_sketch('XY', 'h2')
    k.add_circle('h2', center=[20, 0], radius=5)
    k.close_sketch('h2')
    k.boolean('plate', ['h1', 'h2'], operation='subtract', name='two_holes')
    render(k, "demo2_subtract_multi", OUT / "02_subtract_multi.png",
           "Boolean subtract (multi-tool): 80×50 - 2×Ø10 孔")


def demo3_intersect():
    """3. intersect: 盒 ∩ 圆柱"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'box')
    k.add_rectangle('box', 60, 40, center=[0, 0])
    k.close_sketch('box')
    k.new_sketch('XY', 'cyl')
    k.add_circle('cyl', center=[0, 0], radius=10)
    k.close_sketch('cyl')
    k.boolean('box', ['cyl'], operation='intersect', name='cyl_in_box')
    render(k, "demo3_intersect", OUT / "03_intersect.png",
           "Boolean intersect: 60×40 ∩ Ø20 (理论 圆柱)")


def demo4_l_shape():
    """4. L 形盒用 boolean union 拼接"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'base')
    k.add_rectangle('base', 80, 10, center=[0, 0])
    k.close_sketch('base')
    k.new_sketch('XY', 'upright')
    k.add_rectangle('upright', 10, 60, center=[-35, 35])
    k.close_sketch('upright')
    k.boolean('base', ['upright'], operation='union', name='L_bracket')
    # 加圆角
    k.fillet(2.0, edges='all')
    render(k, "demo4_l_shape", OUT / "04_L_union_fillet.png",
           "L 形支架 = boolean union + fillet r=2")


def main():
    print("=" * 60)
    print("Demo 10: Boolean op（v1.7 新增）")
    print("=" * 60)
    demo1_union()
    demo2_subtract_multi()
    demo3_intersect()
    demo4_l_shape()
    print(f"\n所有输出: {OUT}")


if __name__ == "__main__":
    main()
