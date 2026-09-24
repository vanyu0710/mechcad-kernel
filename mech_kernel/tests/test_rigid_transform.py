
"""Regression tests for validated rigid-transform helpers.

The near-pi branch is critical for assembly poses: axis-angle and matrix
rotations both go through matrix -> build123d axis conversion before export.
"""
from __future__ import annotations

import math

import pytest

from mech_kernel.rigid_transform import (
    _rotation_to_axis_angle,
    apply_rigid_transform,
    axis_angle_to_matrix,
    validate_rotation_matrix,
)


@pytest.mark.parametrize("angle_deg", [179.999, 180.0])
@pytest.mark.parametrize(
    "axis",
    [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0 / math.sqrt(3.0), 1.0 / math.sqrt(3.0), 1.0 / math.sqrt(3.0)),
        (0.1, 0.2, 0.3),
    ],
)
def test_near_pi_rotation_recovers_axis(angle_deg: float, axis: tuple) -> None:
    length = math.sqrt(sum(value * value for value in axis))
    unit = tuple(value / length for value in axis)
    matrix = axis_angle_to_matrix(angle_deg, unit)
    recovered_angle, recovered_axis = _rotation_to_axis_angle(matrix)
    assert abs(recovered_angle - angle_deg) < 1e-4
    dot = sum(a * b for a, b in zip(recovered_axis, unit))
    assert abs(dot - 1.0) < 1e-6


def test_identity_rigid_transform_returns_same_shape() -> None:
    class Shape:
        pass

    shape = Shape()
    result = apply_rigid_transform(
        shape,
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        (0.0, 0.0, 0.0),
    )
    assert result is shape


def test_rotation_matrix_rejects_non_finite_and_reflection() -> None:
    with pytest.raises(ValueError):
        validate_rotation_matrix([[float("nan"), 0, 0], [0, 1, 0], [0, 0, 1]])
    with pytest.raises(ValueError):
        validate_rotation_matrix([[1, 0, 0], [0, 1, 0], [0, 0, -1]])
