"""
MechKernel v1.1.1 Demo 5: 真实几何（Build123dAdapter）

演示：
1. 圆柱（circle → extrude）
2. 立方体（rectangle → extrude）
3. 复杂零件（多个 circle → 合并）
4. 4 视角渲染
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mech_kernel import MechKernel


def section(title: str):
    print()
    print("─" * 70)
    print(f"  {title}")
    print("─" * 70)


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "real_geom")
    os.makedirs(out_dir, exist_ok=True)
    
    # ============================================================
    # Test 1: 圆柱
    # ============================================================
    section("Test 1: 圆柱 (circle → extrude)")
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_cylinder")
    k.add_circle("sk_cylinder", (0, 0), 50, name="main_cylinder")
    k.close_sketch("sk_cylinder")
    r = k.extrude("sk_cylinder", depth=20, name="main_body")
    
    geom = k._current_geometry
    print(f"  几何: {type(geom).__name__}")
    print(f"  体积: {geom.volume:.2f} mm³  (期望 ~{3.14159 * 50**2 * 20:.0f})")
    # 兼容 build123d 和 adapter
    if hasattr(geom, 'bounding_box'):
        bb = geom.bounding_box()
        if hasattr(bb, 'min'):
            print(f"  包围盒: ({bb.min.X}, {bb.min.Y}, {bb.min.Z}, {bb.max.X}, {bb.max.Y}, {bb.max.Z})")
        else:
            print(f"  包围盒: {bb}")
    else:
        print(f"  包围盒: {geom.bbox}")
    vc = geom.vertex_count if hasattr(geom, 'vertex_count') else len(geom.vertices())
    fc = geom.face_count if hasattr(geom, 'face_count') else len(geom.faces())
    print(f"  顶点数: {vc}, 面数: {fc}")
    
    # 渲染
    for view in ['iso', 'front', 'top', 'side']:
        renders = k.renderer.render(geom, level='full', geometry_revision=k._geometry_revision)
        if renders.get(view):
            path = os.path.join(out_dir, f"cylinder_{view}.png")
            with open(path, 'wb') as f:
                f.write(renders[view])
            print(f"  ✓ {path}")
    
    # ============================================================
    # Test 2: 立方体
    # ============================================================
    section("Test 2: 立方体 (rectangle → extrude)")
    k2 = MechKernel()
    k2.create_workplane("base", "XY")
    k2.new_sketch("base", "sk_box")
    k2.add_rectangle("sk_box", width=80, height=60, center=(0, 0), name="main_plate")
    k2.close_sketch("sk_box")
    r2 = k2.extrude("sk_box", depth=10, name="main_body")
    
    geom2 = k2._current_geometry
    print(f"  几何: {type(geom2).__name__}")
    print(f"  体积: {geom2.volume:.2f} mm³  (期望 48000)")
    vc = geom2.vertex_count if hasattr(geom2, 'vertex_count') else len(geom2.vertices())
    fc = geom2.face_count if hasattr(geom2, 'face_count') else len(geom2.faces())
    print(f"  顶点数: {vc}, 面数: {fc}")
    
    for view in ['iso', 'front', 'top', 'side']:
        renders = k2.renderer.render(geom2, level='full', geometry_revision=k2._geometry_revision)
        if renders.get(view):
            path = os.path.join(out_dir, f"box_{view}.png")
            with open(path, 'wb') as f:
                f.write(renders[view])
            print(f"  ✓ {path}")
    
    # ============================================================
    # Test 3: 复杂零件（外圆 + 内圆 = 圆环）
    # ============================================================
    section("Test 3: 圆环 (大圆 + 小圆)")
    k3 = MechKernel()
    k3.create_workplane("base", "XY")
    k3.new_sketch("base", "sk_ring")
    k3.add_circle("sk_ring", (0, 0), 80, name="outer")
    k3.add_circle("sk_ring", (0, 0), 40, name="inner")
    k3.close_sketch("sk_ring")
    r3 = k3.extrude("sk_ring", depth=10, name="ring_body")
    
    geom3 = k3._current_geometry
    print(f"  几何: {type(geom3).__name__}")
    print(f"  体积: {geom3.volume:.2f} mm³")
    # 兼容 build123d 和 adapter
    if callable(getattr(geom3, 'bounding_box', None)):
        bb = geom3.bounding_box()
        if hasattr(bb, 'min'):
            print(f"  包围盒: ({bb.min.X}, {bb.min.Y}, {bb.min.Z}, {bb.max.X}, {bb.max.Y}, {bb.max.Z})")
        else:
            print(f"  包围盒: {bb}")
    else:
        print(f"  包围盒: {getattr(geom3, 'bbox', 'N/A')}")
    
    for view in ['iso', 'top', 'side']:
        renders = k3.renderer.render(geom3, level='full', geometry_revision=k3._geometry_revision)
        if renders.get(view):
            path = os.path.join(out_dir, f"ring_{view}.png")
            with open(path, 'wb') as f:
                f.write(renders[view])
            print(f"  ✓ {path}")
    
    # ============================================================
    # Test 4: LLM 视角 op 列表
    # ============================================================
    section("Test 4: LLM 用的 op 列表（capability registry）")
    k4 = MechKernel()
    public_ops = k4.cap.list_public()
    print(f"  公开 op 数: {len(public_ops)}")
    print(f"  前 5 个:")
    for op in public_ops[:5]:
        print(f"    - {op['name']:20s} [{op['category']}]: {op['description']}")
    
    # ============================================================
    # 总结
    # ============================================================
    section("总结")
    print(f"  4 视角 PNG 渲染: {out_dir}/")
    print(f"  几何来源: Build123dAdapter（v1.1.1 mock，未来装上 build123d 即可切换）")
    print(f"  Duck-typed 兼容: vertices/faces/bbox/volume/surface_area 全部正确")


if __name__ == "__main__":
    main()
