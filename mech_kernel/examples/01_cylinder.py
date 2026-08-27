"""
Demo 1: 创建一个圆柱体（M0 阶段演示数据流）

M0 阶段说明：extrude 只做数据准备 + 类型化错误示范，不实际生成几何。
M2 阶段才会调 build123d 计算。
"""
from mech_kernel import MechKernel


def main():
    print("=" * 60)
    print("MechKernel v1.1 Demo 1: 创建圆柱体（数据流演示）")
    print("=" * 60)
    
    k = MechKernel()
    
    # 1. 创建工作平面
    print("\n[1] create_workplane('base', 'XY')")
    r = k.create_workplane("base", "XY")
    print(f"  ✓ success={r.success}, feature_id={r.feature_id}")
    print(f"  ✓ render_level={r.render_level}（草图类不渲染）")
    print(f"  ✓ narrative: {r.narrative}")
    
    # 2. 创建草图
    print("\n[2] new_sketch('base', 'sk_1')")
    r = k.new_sketch("base", "sk_1")
    print(f"  ✓ success={r.success}, feature_id={r.feature_id}")
    print(f"  ✓ render_level={r.render_level}（草图类不渲染）")
    
    # 3. 添加圆
    print("\n[3] add_circle('sk_1', (0,0), 50)")
    r = k.add_circle("sk_1", (0, 0), 50, name="outer_circle")
    print(f"  ✓ success={r.success}, feature_id={r.feature_id}")
    print(f"  ✓ render_level={r.render_level}（草图类不渲染）")
    
    # 4. 关闭草图
    print("\n[4] close_sketch('sk_1')")
    r = k.close_sketch("sk_1")
    print(f"  ✓ success={r.success}")
    print(f"  ✓ render_level={r.render_level}")
    
    # 5. 拉伸（拓扑变化！）
    print("\n[5] extrude('sk_1', depth=20) ← 拓扑变化，必渲染")
    r = k.extrude("sk_1", depth=20, mode="new_body", name="main_body")
    print(f"  ✓ success={r.success}, feature_id={r.feature_id}")
    print(f"  ✓ render_level={r.render_level}（专家 C 方案：iso_only）")
    print(f"  ✓ has_render: {r.has_render()}（M2 阶段才有 PNG）")
    print(f"  ✓ geometry_summary: feature_count={r.geometry_summary.feature_count}")
    print(f"  ✓ narrative: {r.narrative}")
    
    # 6. 查看状态
    print("\n[6] 当前状态")
    state = k.get_state()
    print(f"  • workplane_count = {state['workplane_count']}")
    print(f"  • sketch_count = {state['sketch_count']}")
    print(f"  • feature_count = {state['feature_count']}")
    print(f"  • narrative 条数 = {len(state['narrative'])}")
    
    # 7. 撤销
    print("\n[7] undo() 撤销拉伸")
    r = k.undo()
    print(f"  ✓ success={r.success}")
    print(f"  ✓ narrative: {r.narrative}")
    print(f"  ✓ feature_count = {k.get_state()['feature_count']}")
    
    # 8. 重做
    print("\n[8] redo() 重做拉伸")
    r = k.redo()
    print(f"  ✓ success={r.success}")
    print(f"  ✓ feature_count = {k.get_state()['feature_count']}")
    
    print("\n" + "=" * 60)
    print("✓ M0 数据流演示完成")
    print("=" * 60)
    print("\n下一步：M1 阶段实现 geometry_inspector + renderer")
    print("       M2 阶段实现 build123d 实际几何计算")


if __name__ == "__main__":
    main()
