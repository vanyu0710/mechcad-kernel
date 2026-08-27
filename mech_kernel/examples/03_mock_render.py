"""
Demo 3: M1 阶段 — 用 MockMesh 展示 3D 渲染

M1 阶段实现：
- geometry_inspector：BRep 指标（体积/面数/包围盒/流形/水密/连通）
- renderer：matplotlib 离屏 4 视角渲染
- adaptive_renderer：智能决定何时渲染
"""
import os
import sys
sys.path.insert(0, '/workspace')

from mech_kernel import MechKernel


class MockBox:
    """Mock 一个 10x10x10 的立方体"""
    def __init__(self):
        self.vertices = [
            (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
            (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
        ]
        self.faces = [
            [0, 1, 2], [0, 2, 3],   # 底
            [4, 5, 6], [4, 6, 7],   # 顶
            [0, 1, 5], [0, 5, 4],   # 前
            [2, 3, 7], [2, 7, 6],   # 后
            [1, 2, 6], [1, 6, 5],   # 右
            [0, 3, 7], [0, 7, 4],   # 左
        ]
        self._volume = 1000
        self._area = 600
    
    def volume(self): return self._volume
    def area(self): return self._area
    def face_count(self): return 12
    def edge_count(self): return 18
    def vertex_count(self): return 8
    def bounding_box(self):
        from types import SimpleNamespace
        bb = SimpleNamespace()
        bb.min = SimpleNamespace(X=0, Y=0, Z=0)
        bb.max = SimpleNamespace(X=10, Y=10, Z=10)
        return bb


def main():
    print("=" * 60)
    print("MechKernel v1.1 Demo 3: M1 渲染 + 几何检查")
    print("=" * 60)
    
    k = MechKernel()
    k._current_geometry = MockBox()  # 注入 mock 几何
    
    # 1. 几何检查
    print("\n[1] Geometry Inspector 检查 mock 立方体")
    summary = k.inspector.summary(k._current_geometry, feature_count=1)
    print(f"  ✓ bounding_box = {summary.bounding_box}")
    print(f"  ✓ volume = {summary.volume} mm³")
    print(f"  ✓ surface_area = {summary.surface_area} mm²")
    print(f"  ✓ face_count = {summary.face_count}")
    print(f"  ✓ edge_count = {summary.edge_count}")
    print(f"  ✓ vertex_count = {summary.vertex_count}")
    print(f"  ✓ is_manifold = {summary.is_manifold}  (Euler: {summary.vertex_count} - {summary.edge_count} + {summary.face_count} = {summary.vertex_count - summary.edge_count + summary.face_count})")
    print(f"  ✓ is_watertight = {summary.is_watertight}")
    print(f"  ✓ is_connected = {summary.is_connected}")
    
    # 2. 渲染：iso 视角
    print("\n[2] 渲染 iso 视角")
    views = k.renderer.render(k._current_geometry, "iso_only")
    if views["iso"]:
        print(f"  ✓ iso PNG 字节数 = {len(views['iso'])}")
        # 保存到文件
        out_path = "/workspace/mech_kernel/examples/mock_box_iso.png"
        with open(out_path, "wb") as f:
            f.write(views["iso"])
        print(f"  ✓ 保存到 {out_path}")
    
    # 3. 渲染：full（4 视角）
    print("\n[3] 渲染 full（4 视角）")
    views = k.renderer.render(k._current_geometry, "full")
    for name in ["iso", "front", "top", "side"]:
        if views.get(name):
            out_path = f"/workspace/mech_kernel/examples/mock_box_{name}.png"
            with open(out_path, "wb") as f:
                f.write(views[name])
            print(f"  ✓ {name}: {len(views[name])} bytes → {out_path}")
    
    # 4. Adaptive Renderer 测试
    print("\n[4] Adaptive Renderer 决策测试")
    ar = k.adaptive_renderer
    ar.reset()
    
    test_sequence = [
        ("create_workplane", True),   # 工作平面
        ("new_sketch", True),         # 草图
        ("add_circle", True),         # 草图元素
        ("close_sketch", True),       # 草图关闭
        ("extrude", True),            # 拓扑变化 → iso
        ("add_circle", True),         # 草图元素
        ("add_rectangle", True),      # 草图元素
        ("add_line", True),           # 草图元素
        ("fillet", True),             # 拓扑变化 → iso
    ]
    
    for op, has_geom in test_sequence:
        level = ar.should_render(op, has_geometry=has_geom)
        print(f"  • {op:25s} → {level}")
    
    # 5. 验证
    print("\n[5] 几何有效性验证")
    is_valid, issues = k.inspector.validate(k._current_geometry)
    print(f"  ✓ is_valid = {is_valid}")
    if issues:
        for issue in issues:
            print(f"  ⚠ {issue}")
    
    print("\n" + "=" * 60)
    print("✓ M1 渲染 + 检查演示完成")
    print("=" * 60)
    print("\nM1 阶段交付：")
    print("  • geometry_inspector.py  (BRep 指标)")
    print("  • renderer.py           (matplotlib 4 视角渲染)")
    print("  • adaptive_renderer.py  (C 方案策略)")
    print("  • 25 个 M1 专项测试")
    print("\n下一步 M2: 集成 build123d 实现真实几何计算")


if __name__ == "__main__":
    main()
