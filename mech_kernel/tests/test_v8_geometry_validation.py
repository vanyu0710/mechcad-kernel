"""v2.6 geometry validation and deterministic fingerprint contracts."""
import json

import pytest

from mech_kernel import MechKernel
from mech_kernel.errors import GeometryValidationError
from mech_kernel.geometry_inspector import GeometryInspector
from mech_kernel.kernel import PUBLIC_OPS
from mech_kernel.transaction import Transaction


def _solid_kernel():
    kernel = MechKernel()
    kernel.create_workplane("base", "XY")
    kernel.new_sketch("base", "plate")
    kernel.add_rectangle("plate", 20, 30)
    kernel.close_sketch("plate")
    return kernel


def test_validate_geometry_is_public_and_schema_aligned():
    kernel = MechKernel()
    assert "validate_geometry" in PUBLIC_OPS
    schema = kernel.cap.get("validate_geometry").input_schema
    assert set(schema) == {"target", "level"}
    assert schema["level"].enum == ["basic", "standard", "strict"]


def test_valid_geometry_result_contains_diagnostic_and_fingerprint():
    kernel = _solid_kernel()
    result = kernel.extrude("plate", 10)
    assert result.success
    assert result.geometry_validation["valid"] is True
    assert result.geometry_validation["status"] == "valid"
    assert result.geometry_validation["fingerprint"].startswith("sha256:")
    checked = kernel.execute("validate_geometry", level="strict")
    assert checked.success
    assert checked.value["fingerprint"] == result.geometry_validation["fingerprint"]


def test_fingerprint_is_deterministic_for_same_metrics():
    kernel = _solid_kernel()
    kernel.extrude("plate", 10)
    inspector = GeometryInspector()
    first = inspector.fingerprint(kernel._current_geometry)
    second = inspector.fingerprint(kernel._current_geometry)
    assert first == second


def test_invalid_candidate_rolls_back_before_commit():
    kernel = _solid_kernel()
    before = kernel._snapshot()

    class EmptyGeometry:
        volume = 0.0
        area = 0.0
        face_count = 0
        edge_count = 0
        vertex_count = 0

        def bounding_box(self):
            class Box:
                min = type("Point", (), {"X": 0.0, "Y": 0.0, "Z": 0.0})()
                max = type("Point", (), {"X": 0.0, "Y": 0.0, "Z": 0.0})()
            return Box()

    with pytest.raises(GeometryValidationError):
        with Transaction(kernel, "extrude") as txn:
            kernel._geometry_internal = EmptyGeometry()
            txn.commit()
    assert kernel._geometry_internal is before["geometry"]
    assert kernel._op_history == before["op_history"]


def test_history_contains_v26_validation(tmp_path):
    kernel = _solid_kernel()
    kernel.extrude("plate", 10)
    paths = kernel.save_project(str(tmp_path / "plate"))
    with open(paths["history_path"], encoding="utf-8") as stream:
        history = json.load(stream)
    assert history["schema_version"] == "2.6"
    assert history["geometry_validation"]["fingerprint"].startswith("sha256:")
