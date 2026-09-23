"""M1 semantic topology selection tests."""
import math
import pytest

from mech_kernel import MechKernel
from mech_kernel.topology_query import measure_topology, query_topology_by_id, select_face_at_point, select_topology_at_point


def _box_kernel():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "s")
    kernel.add_rectangle("s", 20, 10)
    kernel.close_sketch("s")
    kernel.extrude("s", 5)
    return kernel


def _two_parallel_hole_kernel():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "plate")
    kernel.add_rectangle("plate", 40, 20)
    kernel.close_sketch("plate")
    kernel.extrude("plate", 5)
    for index, center in enumerate(((0, 0), (20, 0))):
        sketch = f"hole_{index}"
        kernel.new_sketch("base", sketch)
        kernel.add_circle(sketch, center, 3)
        kernel.close_sketch(sketch)
        kernel.extrude(sketch, 10, mode="cut")
    return kernel


def test_select_top_plane_returns_plane_semantics():
    kernel = _box_kernel()
    result = select_face_at_point(
        kernel._current_geometry,
        (0, 0, 5),
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["source"] == "brep"
    assert result["topology"]["type"] == "face"
    assert result["topology"]["kind"] == "plane"
    # Topology IDs must not leak face-array positions; rebuilds may reorder them.
    assert result["topology"]["id"] == f"face:{result['topology']['fingerprint']}"
    assert result["topology"]["id"].startswith("face:sha256:")
    assert math.isclose(result["geometry"]["area_mm2"], 200.0, abs_tol=1e-5)
    assert result["feature_id"] == "F_0001"
    assert result["hit"]["distance_to_face_mm"] < 0.001


def test_select_far_point_rejects_rather_than_guessing():
    kernel = _box_kernel()
    assert select_face_at_point(kernel._current_geometry, (100, 100, 100)) is None


def test_cylinder_face_returns_radius_and_axis():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "c")
    kernel.add_circle("c", (0, 0), 5)
    kernel.close_sketch("c")
    kernel.extrude("c", 10)
    result = select_face_at_point(
        kernel._current_geometry,
        (5, 0, 5),
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["topology"]["kind"] == "cylinder"
    assert math.isclose(result["geometry"]["radius_mm"], 5.0, abs_tol=1e-5)
    # OCC may orient a cylindrical axis either way; both represent the same
    # physical axis.  Keep the raw oriented axis and assert the unsigned line.
    axis = result["geometry"]["axis"]
    assert math.isclose(abs(axis[2]), 1.0, abs_tol=1e-9)
    assert math.isclose(math.hypot(axis[0], axis[1]), 0.0, abs_tol=1e-9)


def test_camera_ray_selects_visible_face_at_box_edge():
    kernel = _box_kernel()
    # Top-front edge: top face and front face are both distance 0 from the point.
    result = select_face_at_point(
        kernel._current_geometry,
        (0, -5, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    normal = result["hit"]["face_normal"]
    assert normal is not None
    # Looking down from +Z must select the visible top face, not the bottom face.
    assert normal[2] > 0.9
    assert result["hit"]["distance_to_face_mm"] < 0.001


def test_camera_ray_prefers_front_facing_coincident_face():
    kernel = _box_kernel()
    # Front/right edge viewed from +X: both faces are at the point, but only the
    # +X side is front-facing.  The -X side is back-facing and must not win.
    result = select_face_at_point(
        kernel._current_geometry,
        (10, 0, 0),
        feature_geometries=kernel._feature_geometries,
        direction=(1, 0, 0),
    )
    assert result is not None
    normal = result["hit"]["face_normal"]
    assert normal is not None
    assert normal[0] > 0.9


def test_selection_returns_face_level_display_mesh():
    kernel = _box_kernel()
    result = select_face_at_point(
        kernel._current_geometry,
        (0, 0, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    display = result["display"]
    assert display is not None
    assert display["type"] == "triangles"
    assert display["source"] == "brep"
    assert display["triangle_count"] == 2
    assert len(display["vertices"]) == 4
    assert len(display["indices"]) == display["triangle_count"] * 3


def test_topology_selection_prefers_boundary_edge_over_adjacent_face():
    kernel = _box_kernel()
    result = select_topology_at_point(
        kernel._current_geometry,
        (0, -5, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    assert result["topology"]["type"] == "edge"
    assert result["topology"]["kind"] == "line"
    assert math.isclose(result["geometry"]["length_mm"], 20.0, abs_tol=1e-5)
    assert result["display"]["type"] == "polyline"
    assert result["display"]["source"] == "brep"
    assert len(result["display"]["vertices"]) == 2


def test_topology_selection_prefers_exact_vertex():
    kernel = _box_kernel()
    result = select_topology_at_point(
        kernel._current_geometry,
        (-10, -5, 0),
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["topology"]["type"] == "vertex"
    assert result["geometry"]["point"] == [-10.0, -5.0, 0.0]
    assert result["display"]["type"] == "point"
    assert result["hit"]["distance_to_vertex_mm"] < 1e-6


def test_topology_selection_returns_circle_edge_semantics():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "c")
    kernel.add_circle("c", (0, 0), 5)
    kernel.close_sketch("c")
    kernel.extrude("c", 10)
    result = select_topology_at_point(
        kernel._current_geometry,
        (0, 5, 10),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    assert result["topology"]["type"] == "edge"
    assert result["topology"]["kind"] == "circle"
    assert math.isclose(result["geometry"]["radius_mm"], 5.0, abs_tol=1e-5)
    assert result["geometry"]["center"] == [0.0, 0.0, 10.0]
    assert len(result["display"]["vertices"]) > 2


def test_interior_click_still_selects_face():
    kernel = _box_kernel()
    result = select_topology_at_point(
        kernel._current_geometry,
        (0, 0, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    assert result["topology"]["type"] == "face"
    assert result["topology"]["kind"] == "plane"


def test_query_topology_by_id_round_trips_selected_edge():
    kernel = _box_kernel()
    result = select_topology_at_point(
        kernel._current_geometry,
        (0, -5, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    queried = query_topology_by_id(
        kernel._current_geometry,
        result["topology"]["id"],
        feature_geometries=kernel._feature_geometries,
    )
    assert queried is not None
    assert queried["topology"] == result["topology"]
    assert queried["geometry"] == result["geometry"]
    assert queried["display"] == result["display"]
    assert queried["feature_id"] == result["feature_id"]
    assert queried["hit"] is None
    assert queried["source"] == "brep"


def test_query_topology_by_id_round_trips_selected_face():
    kernel = _box_kernel()
    result = select_topology_at_point(
        kernel._current_geometry,
        (0, 0, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert result is not None
    queried = query_topology_by_id(
        kernel._current_geometry,
        result["topology"]["id"],
        feature_geometries=kernel._feature_geometries,
    )
    assert queried is not None
    assert queried["topology"] == result["topology"]
    assert queried["hit"] is None


def test_query_topology_rejects_unstable_index_style_id():
    kernel = _box_kernel()
    with pytest.raises(ValueError, match="face:, edge:, or vertex:"):
        query_topology_by_id(kernel._current_geometry, "F00")


def test_counterbore_brep_semantics_support_engineering_measurements():
    """沉孔验收：小径、大径、肩台深度、轴线都来自可选中 BRep 面。"""
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "plate")
    kernel.add_rectangle("plate", 60, 40)
    kernel.close_sketch("plate")
    kernel.extrude("plate", 10)
    kernel.hole(
        position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
        counterbore_diameter=10, counterbore_depth=3,
    )

    top_before = select_face_at_point(
        kernel._current_geometry,
        (20, 0, 10),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    assert top_before is not None

    # 加入第二个孔会改变顶面 trim/面积，旧语义指纹必须失效而不是重绑到新面。
    kernel.hole(position=(20, 0), diameter=6)
    assert measure_topology(
        kernel._current_geometry,
        [top_before["topology"]["id"]],
        feature_geometries=kernel._feature_geometries,
    ) is None

    large = select_face_at_point(
        kernel._current_geometry,
        (0, 5, 8.5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 1, 0),
    )
    small = select_face_at_point(
        kernel._current_geometry,
        (0, 3, 6),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 1, 0),
    )
    shoulder = select_face_at_point(
        kernel._current_geometry,
        (4, 0, 7),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    top = select_face_at_point(
        kernel._current_geometry,
        (-20, 0, 10),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    reference = select_face_at_point(
        kernel._current_geometry,
        (20, 3, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 1, 0),
    )
    assert all(item is not None for item in (large, small, shoulder, top, reference))
    assert large["geometry"]["radius_mm"] == pytest.approx(5.0)
    assert small["geometry"]["radius_mm"] == pytest.approx(3.0)
    assert shoulder["geometry"]["kind"] == "plane"
    assert top["geometry"]["kind"] == "plane"

    features = kernel._feature_geometries
    large_diameter = measure_topology(
        kernel._current_geometry, [large["topology"]["id"]], feature_geometries=features
    )
    small_diameter = measure_topology(
        kernel._current_geometry, [small["topology"]["id"]], feature_geometries=features
    )
    shoulder_depth = measure_topology(
        kernel._current_geometry,
        [top["topology"]["id"], shoulder["topology"]["id"]],
        feature_geometries=features,
    )
    axis_distance = measure_topology(
        kernel._current_geometry,
        [small["topology"]["id"], reference["topology"]["id"]],
        feature_geometries=features,
    )
    assert large_diameter["metric"] == "diameter"
    assert large_diameter["result"]["distance"] == pytest.approx(10.0, abs=1e-5)
    assert small_diameter["metric"] == "diameter"
    assert small_diameter["result"]["distance"] == pytest.approx(6.0, abs=1e-5)
    assert shoulder_depth["metric"] == "face_to_face"
    assert shoulder_depth["result"]["distance"] == pytest.approx(3.0, abs=0.01)
    assert axis_distance["metric"] == "axis_to_axis"
    assert axis_distance["result"]["distance"] == pytest.approx(20.0, abs=1e-5)
    assert all(result["source"] == "brep" for result in (
        large_diameter, small_diameter, shoulder_depth, axis_distance
    ))


def test_measure_topology_returns_face_to_face_distance_for_parallel_planes():
    kernel = _box_kernel()
    top = select_face_at_point(
        kernel._current_geometry,
        (0, 0, 5),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, -1),
    )
    bottom = select_face_at_point(
        kernel._current_geometry,
        (0, 0, 0),
        feature_geometries=kernel._feature_geometries,
        direction=(0, 0, 1),
    )
    assert top is not None and bottom is not None
    result = measure_topology(
        kernel._current_geometry,
        [top["topology"]["id"], bottom["topology"]["id"]],
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["metric"] == "face_to_face"
    assert math.isclose(result["result"]["distance"], 5.0, abs_tol=1e-5)
    assert result["source"] == "brep"
    assert result["algorithm"] == "occ_analytic_brep"
    # The evidence segment is perpendicular to both planes.
    assert math.isclose(result["result"]["dx"], 0.0, abs_tol=1e-6)
    assert math.isclose(result["result"]["dy"], 0.0, abs_tol=1e-6)
    assert math.isclose(result["result"]["dz"], 5.0, abs_tol=1e-6)


def test_measure_topology_returns_brep_minimum_distance_for_generic_topologies():
    kernel = _box_kernel()
    first = select_topology_at_point(
        kernel._current_geometry,
        (-10, -5, 0),
        feature_geometries=kernel._feature_geometries,
    )
    second = select_topology_at_point(
        kernel._current_geometry,
        (10, 5, 5),
        feature_geometries=kernel._feature_geometries,
    )
    assert first is not None and second is not None
    assert first["topology"]["type"] == "vertex"
    assert second["topology"]["type"] == "vertex"
    result = measure_topology(
        kernel._current_geometry,
        [first["topology"]["id"], second["topology"]["id"]],
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["metric"] == "minimum_distance"
    assert math.isclose(result["result"]["distance"], math.sqrt(525.0), abs_tol=1e-5)
    assert result["source"] == "brep"
    assert result["algorithm"] == "occ_brep_extrema"


def test_axis_to_axis_measure_returns_true_closest_points_for_nonorthogonal_axes():
    from mech_kernel.topology_query import _axis_to_axis_measure

    sqrt2 = math.sqrt(2.0)
    first = {
        "geometry": {
            "kind": "cylinder",
            "origin": [0.0, 0.0, 0.0],
            "axis": [0.0, 0.0, 1.0],
        }
    }
    second = {
        "geometry": {
            "kind": "cylinder",
            "origin": [10.0, 0.0, 5.0],
            "axis": [1.0 / sqrt2, 0.0, 1.0 / sqrt2],
        }
    }
    result = _axis_to_axis_measure(first, second, ["face:a", "face:b"])
    assert result is not None
    # These two infinite axes intersect at (0, 0, -5).
    assert math.isclose(result["result"]["distance"], 0.0, abs_tol=1e-7)
    assert result["result"]["p1"] == pytest.approx([0.0, 0.0, -5.0], abs=1e-7)
    assert result["result"]["p2"] == pytest.approx([0.0, 0.0, -5.0], abs=1e-7)


def test_axis_to_axis_measure_returns_perpendicular_offset_for_parallel_axes_with_shifted_origins():
    from mech_kernel.topology_query import _axis_to_axis_measure

    first = {
        "geometry": {
            "kind": "cylinder",
            "origin": [0.0, 0.0, 0.0],
            "axis": [0.0, 0.0, 1.0],
        }
    }
    second = {
        "geometry": {
            "kind": "cylinder",
            "origin": [3.0, 4.0, 19.0],
            "axis": [0.0, 0.0, -2.0],
        }
    }
    result = _axis_to_axis_measure(first, second, ["face:a", "face:b"])
    assert result is not None
    assert math.isclose(result["result"]["distance"], 5.0, abs_tol=1e-7)
    assert result["result"]["p1"] == pytest.approx([0.0, 0.0, 19.0], abs=1e-7)
    assert result["result"]["p2"] == pytest.approx([3.0, 4.0, 19.0], abs=1e-7)
    assert math.isclose(result["result"]["dx"], 3.0, abs_tol=1e-7)
    assert math.isclose(result["result"]["dy"], 4.0, abs_tol=1e-7)
    assert math.isclose(result["result"]["dz"], 0.0, abs_tol=1e-7)


def test_measure_topology_returns_axis_to_axis_distance_for_parallel_cylinders():
    kernel = _two_parallel_hole_kernel()
    first = select_face_at_point(
        kernel._current_geometry,
        (3, 0, 4.5),
        feature_geometries=kernel._feature_geometries,
        direction=(1, 0, 0),
    )
    second = select_face_at_point(
        kernel._current_geometry,
        (17, 0, 4.5),
        feature_geometries=kernel._feature_geometries,
        direction=(-1, 0, 0),
    )
    assert first is not None and second is not None
    assert first["topology"]["kind"] == "cylinder"
    assert second["topology"]["kind"] == "cylinder"
    result = measure_topology(
        kernel._current_geometry,
        [first["topology"]["id"], second["topology"]["id"]],
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["metric"] == "axis_to_axis"
    assert math.isclose(result["result"]["distance"], 20.0, abs_tol=1e-5)
    assert result["source"] == "brep"
    assert result["algorithm"] == "occ_analytic_brep"
    assert math.isclose(result["result"]["dx"], 20.0, abs_tol=1e-6)
    assert math.isclose(result["result"]["dy"], 0.0, abs_tol=1e-6)
    assert math.isclose(result["result"]["dz"], 0.0, abs_tol=1e-6)


def test_measure_topology_returns_cylinder_diameter():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "c")
    kernel.add_circle("c", (0, 0), 5)
    kernel.close_sketch("c")
    kernel.extrude("c", 10)
    selected = select_face_at_point(
        kernel._current_geometry,
        (5, 0, 5),
        feature_geometries=kernel._feature_geometries,
    )
    assert selected is not None
    result = measure_topology(
        kernel._current_geometry,
        [selected["topology"]["id"]],
        feature_geometries=kernel._feature_geometries,
    )
    assert result is not None
    assert result["metric"] == "diameter"
    assert math.isclose(result["result"]["distance"], 10.0, abs_tol=1e-5)
    assert result["source"] == "brep"
    assert result["algorithm"] == "occ_analytic_brep"
