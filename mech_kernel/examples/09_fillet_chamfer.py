"""
Demo 09: Fillet + Chamfer 在真实零件上的应用

4 个 demo:
1. 圆角立方体（基本 fillet）
2. 倒角方板（基本 chamfer）
3. 带沉孔 + 圆角（fillet 复合 boolean）
4. 法兰盘带圆角（fillet 圆盘边缘 + 沉孔边）

每个 demo 输出对比图（带 fillet/chamfer 前 vs 后）
"""
from __future__ import annotations
import os, sys, math
from pathlib import Path
HERE = Path(__file__).parent
OUT = HERE / "fillet_out"
OUT.mkdir(exist_ok=True)


def render_compare(kernel_before, kernel_after, name, kind, out_path):
    """对比图：左 = 原始 / 右 = fillet 或 chamfer 后"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mech_kernel.renderer import Renderer
    from PIL import Image
    import io
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    r = Renderer()
    if kernel_before._current_geometry is None:
        print(f"[WARN] {name} before 几何为空")
        return
    if kernel_after._current_geometry is None:
        print(f"[WARN] {name} after 几何为空")
        return
    
    before_views = r.render(kernel_before._current_geometry, "iso_only",
                            geometry_revision=kernel_before._geometry_revision)
    after_views = r.render(kernel_after._current_geometry, "iso_only",
                           geometry_revision=kernel_after._geometry_revision)
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    if before_views.get("iso"):
        axes[0].imshow(Image.open(io.BytesIO(before_views["iso"])))
    axes[0].set_title("Before", fontsize=14, weight="bold")
    axes[0].axis("off")
    if after_views.get("iso"):
        axes[1].imshow(Image.open(io.BytesIO(after_views["iso"])))
    axes[1].set_title(f"After {kind}", fontsize=14, weight="bold")
    axes[1].axis("off")
    
    v_before = kernel_before._current_geometry.volume
    v_after = kernel_after._current_geometry.volume
    diff = v_before - v_after
    plt.suptitle(f"{name} | {v_before:.1f} → {v_after:.1f} mm³ (Δ = {diff:.1f})", fontsize=13, weight="bold")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] {name}: {v_before:.1f} → {v_after:.1f} mm³ (Δ = {diff:.1f})")


def demo1_box_fillet():
    """1. 立方体 fillet r=2 — 基本圆角"""
    from mech_kernel import MechKernel
    before = MechKernel()
    before.create_workplane('XY', 'XY')
    before.new_sketch('XY', 'sk')
    before.add_rectangle('sk', 40, 30, center=[0, 0])
    before.close_sketch('sk')
    before.extrude('sk', depth=20, mode='new_body', name='box')
    
    after = MechKernel()
    after.create_workplane('XY', 'XY')
    after.new_sketch('XY', 'sk')
    after.add_rectangle('sk', 40, 30, center=[0, 0])
    after.close_sketch('sk')
    after.extrude('sk', depth=20, mode='new_body', name='box')
    after.fillet(2.0, edges='all')
    
    render_compare(before, after, "demo1_box_fillet", "fillet r=2",
                   OUT / "01_box_fillet.png")


def demo2_plate_chamfer():
    """2. 薄板 chamfer l=1.5 — 倒角边"""
    from mech_kernel import MechKernel
    before = MechKernel()
    before.create_workplane('XY', 'XY')
    before.new_sketch('XY', 'sk')
    before.add_rectangle('sk', 80, 60, center=[0, 0])
    before.close_sketch('sk')
    before.extrude('sk', depth=5, mode='new_body', name='plate')
    
    after = MechKernel()
    after.create_workplane('XY', 'XY')
    after.new_sketch('XY', 'sk')
    after.add_rectangle('sk', 80, 60, center=[0, 0])
    after.close_sketch('sk')
    after.extrude('sk', depth=5, mode='new_body', name='plate')
    after.chamfer(1.5, edges='all')
    
    render_compare(before, after, "demo2_plate_chamfer", "chamfer l=1.5",
                   OUT / "02_plate_chamfer.png")


def demo3_plate_hole_fillet():
    """3. 带沉孔板 + 圆角孔边（fillet after boolean cut）"""
    from mech_kernel import MechKernel
    before = MechKernel()
    before.create_workplane('XY', 'XY')
    before.new_sketch('XY', 'sk')
    before.add_rectangle('sk', 80, 60, center=[0, 0])
    before.close_sketch('sk')
    before.extrude('sk', depth=10, mode='new_body', name='plate')
    # 中心 Ø20 通孔
    before.new_sketch('XY', 'hole')
    before.add_circle('hole', center=[0, 0], radius=10)
    before.close_sketch('hole')
    before.extrude('hole', depth=15, mode='cut', name='hole')
    
    after = MechKernel()
    after.create_workplane('XY', 'XY')
    after.new_sketch('XY', 'sk')
    after.add_rectangle('sk', 80, 60, center=[0, 0])
    after.close_sketch('sk')
    after.extrude('sk', depth=10, mode='new_body', name='plate')
    after.new_sketch('XY', 'hole')
    after.add_circle('hole', center=[0, 0], radius=10)
    after.close_sketch('hole')
    after.extrude('hole', depth=15, mode='cut', name='hole')
    after.fillet(1.5, edges='all')
    
    render_compare(before, after, "demo3_plate_hole_fillet", "fillet r=1.5",
                   OUT / "03_plate_hole_fillet.png")


def demo4_flange_full():
    """4. 完整法兰盘：6 孔 + 中心孔 + 圆角边缘 + 倒角孔边"""
    from mech_kernel import MechKernel
    import math
    after = MechKernel()
    after.create_workplane('XY', 'XY')
    after.new_sketch('XY', 'disc')
    after.add_circle('disc', center=[0, 0], radius=50)
    after.close_sketch('disc')
    after.extrude('disc', depth=10, mode='new_body', name='disc')
    
    # 6 孔
    after.new_sketch('XY', 'holes')
    for i in range(6):
        angle = i * 60 * math.pi / 180
        x = 35 * math.cos(angle)
        y = 35 * math.sin(angle)
        after.add_circle('holes', center=[x, y], radius=4)
    after.close_sketch('holes')
    after.extrude('holes', depth=10, mode='cut', name='holes')
    
    # 中心孔
    after.new_sketch('XY', 'center_hole')
    after.add_circle('center_hole', center=[0, 0], radius=8)
    after.close_sketch('center_hole')
    after.extrude('center_hole', depth=10, mode='cut', name='center_hole')
    
    # fillet 全部边
    after.fillet(1.5, edges='all')
    
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mech_kernel.renderer import Renderer
    from PIL import Image
    import io
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    r = Renderer()
    views = r.render(after._current_geometry, "full",
                     geometry_revision=after._geometry_revision)
    
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for i, vn in enumerate(['iso', 'top', 'front']):
        if views.get(vn):
            axes[i].imshow(Image.open(io.BytesIO(views[vn])))
        axes[i].set_title(vn, fontsize=14, weight="bold")
        axes[i].axis("off")
    
    vol = after._current_geometry.volume
    expected = math.pi * 50**2 * 10 - 7 * math.pi * 4**2 * 10
    plt.suptitle(f"完整法兰盘 (Ø100×10 + 6 孔 + 中心孔 + fillet r=1.5) | vol={vol:.1f} (理论未圆角 {expected:.1f})",
                 fontsize=12, weight="bold")
    plt.tight_layout()
    plt.savefig(OUT / "04_flange_full.png", dpi=110, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"[OK] 法兰盘 vol: {vol:.2f} (理论未圆角 {expected:.1f})")


def main():
    print("=" * 60)
    print("Demo 09: Fillet + Chamfer（v1.4 新能力）")
    print("=" * 60)
    demo1_box_fillet()
    demo2_plate_chamfer()
    demo3_plate_hole_fillet()
    demo4_flange_full()
    print(f"\n所有输出: {OUT}")


if __name__ == "__main__":
    main()
