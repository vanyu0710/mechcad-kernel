"""
Demo 11: Hole + Mirror + Linear Pattern（v1.8-1.10 新增）

4 个工程件:
1. 法兰盘：4 孔用 hole
2. 对称键槽：用 mirror
3. 散热板：8 孔用 linear_pattern
4. 综合：板 + 4 hole + mirror + fillet（完整流程）
"""
from __future__ import annotations
import math
from pathlib import Path
HERE = Path(__file__).parent
OUT = HERE / "hole_out"
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


def demo1_hole():
    """1. 法兰盘：4 角各打一孔"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'flange')
    k.add_rectangle('flange', 80, 80, center=[0, 0])
    k.close_sketch('flange')
    k.extrude('flange', depth=10, mode='new_body', name='flange')
    # 4 角各打一孔
    for x, y in [(-30, -30), (30, -30), (-30, 30), (30, 30)]:
        k.hole(position=(x, y), diameter=10)
    render(k, "demo1_hole", OUT / "01_hole_4corners.png",
           "4 hole @ corners (法兰盘)")


def demo2_mirror():
    """2. 板 + 2 镜像孔（一个孔 + mirror 复制到对面）"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'plate')
    k.add_rectangle('plate', 80, 40, center=[0, 0])
    k.close_sketch('plate')
    k.extrude('plate', depth=10, mode='new_body', name='plate')
    # 1 个孔，Y 镜像到对面
    k.new_sketch('XY', 'left_hole')
    k.add_circle('left_hole', center=[-20, 0], radius=5)
    k.close_sketch('left_hole')
    k.mirror('left_hole', axis='Y', mode='cut')
    render(k, "demo2_mirror", OUT / "02_mirror.png",
           "1 hole + Y-mirror cut (左右对称 2 孔)")


def demo3_linear_pattern():
    """3. 散热板：8 孔 linear_pattern 沿 X 间距 12"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'plate')
    k.add_rectangle('plate', 120, 30, center=[0, 0])
    k.close_sketch('plate')
    k.extrude('plate', depth=8, mode='new_body', name='plate')
    # 1 个孔
    k.new_sketch('XY', 'h')
    k.add_circle('h', center=[0, 0], radius=3)
    k.close_sketch('h')
    # 沿 X 间距 12
    k.linear_pattern('h', count=8, direction=(1, 0), spacing=15, mode='cut')
    render(k, "demo3_linear_pattern", OUT / "03_linear_pattern.png",
           "linear_pattern: 8 holes 沿 X 间距 15")


def demo4_full():
    """4. 综合：板 + 4 hole + mirror + fillet"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'bracket')
    k.add_rectangle('bracket', 60, 60, center=[0, 0])
    k.close_sketch('bracket')
    k.extrude('bracket', depth=10, mode='new_body', name='bracket')
    # 中心 1 大孔（Ø30 沉 5）
    k.hole(position=(0, 0), diameter=15, hole_type='counterbore',
           counterbore_diameter=30, counterbore_depth=5)
    # 4 角小孔
    for x, y in [(-20, -20), (20, -20), (-20, 20), (20, 20)]:
        k.hole(position=(x, y), diameter=5)
    # Y-mirror 上面 2 孔到下面
    k.new_sketch('XY', 'top_hole')
    k.add_circle('top_hole', center=[0, 25], radius=3)
    k.close_sketch('top_hole')
    k.mirror('top_hole', axis='X', mode='cut')
    # 外轮廓 4 条竖直边圆角 (v2.11: select 引用 → fillet 指定边)
    sel = k.select(element_type='edge', filter_type='line')
    v_refs = [e['ref'] for e in sel.value['selected'] if abs(e['length_mm'] - 10) < 1e-6]
    k.fillet(1.5, edges=v_refs)
    render(k, "demo4_full", OUT / "04_full.png",
           "完整流程: 板 + counterbore + 4 hole + mirror + fillet")


def main():
    print("=" * 60)
    print("Demo 11: Hole + Mirror + Linear Pattern（v1.8-1.10）")
    print("=" * 60)
    demo1_hole()
    demo2_mirror()
    demo3_linear_pattern()
    demo4_full()
    print(f"\n所有输出: {OUT}")


if __name__ == "__main__":
    main()
