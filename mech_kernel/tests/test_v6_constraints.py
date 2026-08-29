"""v2.4 constraint parameterization tests."""
import json
import math
import os
import tempfile

from mech_kernel import ConstraintStatus, InvalidRequestError, MechKernel
from mech_kernel.kernel import PUBLIC_OPS


def _sketch_with_lines():
    kernel = MechKernel()
    kernel.create_workplane("base")
    kernel.new_sketch("base", "profile")
    first = kernel.add_line("profile", (0, 0), (10, 2))
    second = kernel.add_line("profile", (10, 2), (20, 2))
    return kernel, first.feature_id, second.feature_id


def test_constraint_ops_are_public_and_schema_aligned():
    kernel = MechKernel()
    assert {"add_constraint", "set_parameter", "solve_sketch"} <= set(PUBLIC_OPS)
    assert {"add_constraint", "set_parameter", "solve_sketch"} <= set(kernel.cap._caps)
    assert set(kernel.cap.get("add_constraint").input_schema) == {
        "sketch_name", "constraint_type", "references", "value", "parameter_name", "name",
    }


def test_core_line_constraints_solve_deterministically():
    kernel, first, second = _sketch_with_lines()
    horizontal = kernel.add_constraint("profile", "horizontal", [{"entity_id": first, "role": "line"}])
    coincident = kernel.add_constraint("profile", "coincident", [
        {"entity_id": first, "point": "end"},
        {"entity_id": second, "point": "start"},
    ])
    perpendicular = kernel.add_constraint("profile", "perpendicular", [
        {"entity_id": first, "role": "line"},
        {"entity_id": second, "role": "line"},
    ])
    assert horizontal.constraint_diagnostics["status"] == "under_constrained"
    assert coincident.constraint_diagnostics["constraint_count"] == 2
    assert perpendicular.constraint_diagnostics["residual"] < 1e-6
    assert [entry["op"] for entry in kernel._op_history][-3:] == [
        "add_constraint", "add_constraint", "add_constraint",
    ]


def test_distance_parameter_updates_and_replays_downstream_geometry():
    kernel = MechKernel()
    kernel.create_workplane("base")
    kernel.new_sketch("base", "circle")
    circle = kernel.add_circle("circle", (0, 0), 5)
    constraint = kernel.add_constraint(
        "circle", "radius", [{"entity_id": circle.feature_id, "role": "circle"}],
        value=5, parameter_name="radius",
    )
    kernel.close_sketch("circle")
    solid = kernel.extrude("circle", 10, name="body")
    before = kernel.query("_current_geometry", "volume").value
    changed = kernel.set_parameter("radius", 10)
    after = kernel.query("_current_geometry", "volume").value
    assert changed.success
    assert after / before == pytest_approx(4.0, 1e-4)
    assert kernel._op_history[-1]["op"] == "set_parameter"
    assert kernel._op_history[-2]["op"] == "extrude"
    assert solid.feature_id in kernel._feature_geometries
    assert constraint.feature_id in [entry.get("feature_id") for entry in kernel._op_history]


def test_conflicting_constraint_rolls_back_transaction():
    kernel = MechKernel()
    kernel.create_workplane("base")
    kernel.new_sketch("base", "circle")
    circle = kernel.add_circle("circle", (0, 0), 5)
    kernel.add_constraint("circle", "radius", [{"entity_id": circle.feature_id, "role": "circle"}], value=5)
    history_count = len(kernel._op_history)
    try:
        kernel.add_constraint("circle", "radius", [{"entity_id": circle.feature_id, "role": "circle"}], value=6)
    except InvalidRequestError:
        pass
    else:
        raise AssertionError("expected conflicting radius to fail")
    assert len(kernel._op_history) == history_count
    assert len(kernel.sketches["circle"].constraints) == 1
    assert kernel.sketches["circle"].constraints[0].value == 5.0


def test_best_effort_returns_diagnostics_for_conflict():
    kernel, first, _ = _sketch_with_lines()
    kernel.add_constraint("profile", "horizontal", [{"entity_id": first, "role": "line"}])
    kernel.sketches["profile"].constraints.append(
        type(kernel.sketches["profile"].constraints[0])(
            id="C_manual", type="vertical", references=[{"entity_id": first, "role": "line"}],
        )
    )
    result = kernel.solve_sketch("profile", mode="best_effort")
    assert not result.success
    assert result.error_kind == "RECOVERABLE"
    assert result.constraint_diagnostics["status"] in ("conflict", "over_constrained")


def test_undo_redo_restores_constraint_state():
    kernel, first, _ = _sketch_with_lines()
    result = kernel.add_constraint("profile", "horizontal", [{"entity_id": first, "role": "line"}])
    assert len(kernel.sketches["profile"].constraints) == 1
    kernel.undo()
    assert len(kernel.sketches["profile"].constraints) == 0
    kernel.redo()
    assert len(kernel.sketches["profile"].constraints) == 1
    assert kernel.sketches["profile"].constraints[0].id == result.feature_id


def test_sketch_render_and_persistence_include_constraints():
    kernel, first, _ = _sketch_with_lines()
    kernel.add_constraint("profile", "horizontal", [{"entity_id": first, "role": "line"}], name="水平")
    rendered = kernel.render(intent="sketch", target="profile", size=160)
    assert rendered.success
    assert rendered.render_base64
    assert rendered.evidence_manifest["intent"] == "sketch"
    with tempfile.TemporaryDirectory() as tmp:
        kernel.close_sketch("profile")
        kernel.extrude("profile", 4)
        paths = kernel.save_project(os.path.join(tmp, "constraint_part"))
        assert os.path.exists(paths["history_path"])
        with open(paths["history_path"], encoding="utf-8") as stream:
            history = json.load(stream)
            assert history["schema_version"] == "2.6"
        assert any(item["op"] == "add_constraint" for item in history["op_history"])


def pytest_approx(value, tolerance):
    return _Approx(value, tolerance)


class _Approx:
    def __init__(self, value, tolerance):
        self.value = value
        self.tolerance = tolerance

    def __eq__(self, other):
        return math.isclose(float(other), self.value, rel_tol=self.tolerance, abs_tol=self.tolerance)
