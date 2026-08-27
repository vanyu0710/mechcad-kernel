"""
Demo 12: Query / Select / Measure (v1.11-1.15)

5 个 query 能力 + select 按类型 + measure 3 种度量 + delete/update
"""
from __future__ import annotations
from pathlib import Path
HERE = Path(__file__).parent
OUT = HERE / "query_out"
OUT.mkdir(exist_ok=True)


def make_bracket():
    """造一个测试件：带孔的板"""
    from mech_kernel import MechKernel
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'plate')
    k.add_rectangle('plate', 80, 60, center=[0, 0])
    k.close_sketch('plate')
    k.extrude('plate', depth=15, mode='new_body', name='plate')
    k.hole(position=(0, 0), diameter=20)
    return k


def demo1_query():
    """1. Query 6 种属性"""
    k = make_bracket()
    print("=" * 60)
    print("Demo 12-1: Query 6 种属性")
    print("=" * 60)
    for what in ['bounding_box', 'volume', 'centroid', 'face_count', 'edge_count', 'vertex_count']:
        r = k.query('_current_geometry', what)
        print(f'  {what:15s}: {r.value}')


def demo2_select():
    """2. Select 按几何类型"""
    k = make_bracket()
    print("\n" + "=" * 60)
    print("Demo 12-2: Select 按类型")
    print("=" * 60)
    r = k.select('all')
    print(f'  all: total={r.value["total"]}, by_type={r.value["by_type"]}')
    r = k.select('plane')
    print(f'  plane: {len(r.value["selected"])} 个平面')
    r = k.select('cylinder')
    print(f'  cylinder: {len(r.value["selected"])} 个圆柱面（含孔）')


def demo3_measure():
    """3. Measure 3 种度量"""
    k = make_bracket()
    print("\n" + "=" * 60)
    print("Demo 12-3: Measure")
    print("=" * 60)
    r = k.measure('current', metric='volume')
    print(f'  volume: {r.value["volume"]:.2f} mm³')
    r = k.measure('current', metric='area')
    print(f'  area: {r.value["area"]:.2f} mm² (总表面积)')
    r = k.measure('(0, 0, 0)', '(50, 30, 7.5)', metric='distance')
    print(f'  distance: {r.value["distance"]:.2f} mm (两点之间)')


def demo4_variants():
    """4. 对比 4 个变体的 query 结果"""
    from mech_kernel import MechKernel
    print("\n" + "=" * 60)
    print("Demo 12-4: Query 对比 — 4 个变体的几何属性")
    print("=" * 60)
    
    variants = []
    
    # A: 纯板
    k = MechKernel()
    k.create_workplane('XY', 'XY')
    k.new_sketch('XY', 'p')
    k.add_rectangle('p', 60, 40, center=[0, 0])
    k.close_sketch('p')
    k.extrude('p', depth=10, mode='new_body', name='p')
    variants.append(('板 60×40×10', k))
    
    # B: + 1 简单孔
    k2 = MechKernel()
    k2.create_workplane('XY', 'XY')
    k2.new_sketch('XY', 'p')
    k2.add_rectangle('p', 60, 40, center=[0, 0])
    k2.close_sketch('p')
    k2.extrude('p', depth=10, mode='new_body', name='p')
    k2.hole(position=(0, 0), diameter=20)
    variants.append(('+ 1 孔 Ø20', k2))
    
    # C: + 沉孔
    k3 = MechKernel()
    k3.create_workplane('XY', 'XY')
    k3.new_sketch('XY', 'p')
    k3.add_rectangle('p', 60, 40, center=[0, 0])
    k3.close_sketch('p')
    k3.extrude('p', depth=10, mode='new_body', name='p')
    k3.hole(position=(0, 0), diameter=10, hole_type='counterbore', counterbore_diameter=20, counterbore_depth=5)
    variants.append(('+ 沉孔', k3))
    
    # D: + 2 孔 + linear_pattern
    k4 = MechKernel()
    k4.create_workplane('XY', 'XY')
    k4.new_sketch('XY', 'p')
    k4.add_rectangle('p', 100, 30, center=[0, 0])
    k4.close_sketch('p')
    k4.extrude('p', depth=8, mode='new_body', name='p')
    k4.new_sketch('XY', 'h')
    k4.add_circle('h', center=[0, 0], radius=3)
    k4.close_sketch('h')
    k4.linear_pattern('h', count=8, direction=(1, 0), spacing=10, mode='cut')
    variants.append(('板 + 8 孔 pattern', k4))
    
    # 报告
    print(f"\n{'变体':<25} {'Vol (mm³)':<12} {'Faces':<8} {'Edges':<8} {'Verts':<8}")
    print("-" * 70)
    for name, kk in variants:
        vol = kk.query('_current_geometry', 'volume').value
        f = kk.query('_current_geometry', 'face_count').value
        e = kk.query('_current_geometry', 'edge_count').value
        v = kk.query('_current_geometry', 'vertex_count').value
        print(f"{name:<25} {vol:<12.1f} {f:<8} {e:<8} {v:<8}")


def main():
    print("=" * 60)
    print("Demo 12: Query / Select / Measure（v1.11-1.15）")
    print("=" * 60)
    demo1_query()
    demo2_select()
    demo3_measure()
    demo4_variants()
    print(f"\n所有输出: {OUT}")


if __name__ == "__main__":
    main()
