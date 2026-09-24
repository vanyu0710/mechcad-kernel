"""Small, validated rigid-transform helpers shared by gear placement and assembly.

Only proper rigid transforms are accepted: finite vectors, an orthonormal rotation
matrix with determinant +1, and a finite translation.  The helpers deliberately do
not implement mesh solving, kinematics, or constraint propagation.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

Vector3 = tuple[float, float, float]
Matrix3 = tuple[Vector3, Vector3, Vector3]

_EPS = 1e-6


def _finite_vector(name: str, value: Any) -> Vector3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must be a 3-element list/tuple")
    try:
        vector = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} components must be finite numbers") from exc
    if not all(math.isfinite(component) for component in vector):
        raise ValueError(f"{name} components must be finite numbers")
    return vector


def _normalize(name: str, value: Any) -> Vector3:
    vector = _finite_vector(name, value)
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-12:
        raise ValueError(f"{name} must be a non-zero vector")
    return tuple(component / length for component in vector)  # type: ignore[return-value]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: Sequence[float], b: Sequence[float]) -> Vector3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _determinant(m: Sequence[Sequence[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def validate_rotation_matrix(value: Any) -> Matrix3:
    """Validate a 3x3 row-major proper rotation matrix."""
    rows: list[list[float]]
    if isinstance(value, (list, tuple)):
        if len(value) == 9:
            flat = value
            rows = [list(flat[index:index + 3]) for index in (0, 3, 6)]
        elif len(value) == 3 and all(isinstance(row, (list, tuple)) and len(row) == 3 for row in value):
            rows = [list(row) for row in value]
        else:
            rows = []
    else:
        rows = []

    if not rows:
        raise ValueError("rotation_matrix must be a 3x3 nested list or 9-element row-major list")

    try:
        matrix = tuple(tuple(float(component) for component in row) for row in rows)
    except (TypeError, ValueError) as exc:
        raise ValueError("rotation_matrix components must be finite numbers") from exc
    if not all(math.isfinite(component) for row in matrix for component in row):
        raise ValueError("rotation_matrix components must be finite numbers")

    for index, row in enumerate(matrix):
        if abs(math.sqrt(_dot(row, row)) - 1.0) > _EPS:
            raise ValueError(f"rotation_matrix row {index + 1} must have unit length")
    for i in range(3):
        for j in range(i + 1, 3):
            if abs(_dot(matrix[i], matrix[j])) > _EPS:
                raise ValueError(f"rotation_matrix rows {i + 1} and {j + 1} must be orthogonal")
    determinant = _determinant(matrix)
    if abs(determinant - 1.0) > _EPS:
        raise ValueError("rotation_matrix must be a proper rotation (determinant +1, no scaling/reflection)")
    return matrix


def axis_angle_to_matrix(angle_deg: Any, axis: Any) -> Matrix3:
    """Convert `[angle_deg, [x,y,z]]` to a proper rotation matrix."""
    if not isinstance(angle_deg, (int, float)) or not math.isfinite(float(angle_deg)):
        raise ValueError("rotation angle must be a finite number")
    if not isinstance(axis, (list, tuple)) or len(axis) != 3:
        raise ValueError("rotation axis must be a 3-element list/tuple")
    unit = _normalize("rotation axis", axis)
    angle = math.radians(float(angle_deg))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    one_minus_cos = 1.0 - cos_a
    x, y, z = unit
    return (
        (cos_a + x * x * one_minus_cos,
         x * y * one_minus_cos - z * sin_a,
         x * z * one_minus_cos + y * sin_a),
        (y * x * one_minus_cos + z * sin_a,
         cos_a + y * y * one_minus_cos,
         y * z * one_minus_cos - x * sin_a),
        (z * x * one_minus_cos - y * sin_a,
         z * y * one_minus_cos + x * sin_a,
         cos_a + z * z * one_minus_cos),
    )


def orientation_from_axis(axis: Any, reference_direction: Any = None) -> Matrix3:
    """Build a local-to-world orientation whose local +Z follows ``axis``.

    ``reference_direction`` is the desired world direction for local +X.  It is
    projected onto the plane normal to ``axis`` and must not be parallel to it.
    The default remains local +X = world +X when possible.
    """
    z_axis = _normalize("axis", axis)
    if reference_direction is None:
        candidate: Vector3 = (1.0, 0.0, 0.0) if abs(z_axis[0]) <= 0.9 else (0.0, 1.0, 0.0)
    else:
        candidate = _normalize("reference_direction", reference_direction)
    x_axis = tuple(
        candidate[index] - _dot(candidate, z_axis) * z_axis[index]
        for index in range(3)
    )
    x_length = math.sqrt(_dot(x_axis, x_axis))
    if x_length <= 1e-9:
        raise ValueError("reference_direction must not be parallel to axis")
    x_axis = tuple(component / x_length for component in x_axis)
    y_axis = _cross(z_axis, x_axis)
    # Rows are the world images of local x/y/z basis vectors.
    return (
        (x_axis[0], y_axis[0], z_axis[0]),
        (x_axis[1], y_axis[1], z_axis[1]),
        (x_axis[2], y_axis[2], z_axis[2]),
    )


def _rotation_to_axis_angle(rotation: Matrix3) -> tuple[float, Vector3]:
    """Convert a validated proper rotation matrix to angle/axis for build123d."""
    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    trace = max(-1.0, min(3.0, trace))
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))
    if abs(angle) <= 1e-10:
        return 0.0, (1.0, 0.0, 0.0)

    # Near pi, the skew-symmetric term is numerically degenerate.  Extract the
    # axis from the symmetric part using the most stable diagonal as pivot:
    # R[ii] ~= 2*u[i]^2 - 1, and R[ji]+R[ij] ~= 4*u[i]*u[j].
    if 180.0 - angle <= 1e-4:
        pivot = max(range(3), key=lambda index: rotation[index][index])
        diagonal_component = math.sqrt(max(0.0, (rotation[pivot][pivot] + 1.0) / 2.0))
        if diagonal_component <= 1e-12:
            # Defensive fallback for a malformed matrix that passed tolerance.
            vector = (
                rotation[2][1] - rotation[1][2],
                rotation[0][2] - rotation[2][0],
                rotation[1][0] - rotation[0][1],
            )
            return angle, _normalize("rotation axis", vector)
        axis = [0.0, 0.0, 0.0]
        axis[pivot] = diagonal_component
        for other in range(3):
            if other == pivot:
                continue
            axis[other] = (
                rotation[other][pivot] + rotation[pivot][other]
            ) / (4.0 * diagonal_component)
        axis_length = math.sqrt(sum(component * component for component in axis))
        if axis_length <= 1e-12:
            raise ValueError("rotation axis must be a non-zero vector")
        axis = [component / axis_length for component in axis]
        # For angles just below pi, the skew term still carries the axis sign.
        skew = (
            rotation[2][1] - rotation[1][2],
            rotation[0][2] - rotation[2][0],
            rotation[1][0] - rotation[0][1],
        )
        if sum(skew[index] * axis[index] for index in range(3)) < 0.0:
            axis = [-component for component in axis]
        return angle, (axis[0], axis[1], axis[2])

    vector = (
        rotation[2][1] - rotation[1][2],
        rotation[0][2] - rotation[2][0],
        rotation[1][0] - rotation[0][1],
    )
    return angle, _normalize("rotation axis", vector)


def is_identity(rotation: Matrix3, translation: Vector3 = (0.0, 0.0, 0.0)) -> bool:
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    return (all(abs(rotation[i][j] - identity[i][j]) <= 1e-12
                for i in range(3) for j in range(3))
            and all(abs(translation[i]) <= 1e-12 for i in range(3)))


def apply_rigid_transform(shape: Any, rotation: Matrix3, translation: Vector3) -> Any:
    """Apply a proper rotate-then-translate transform to a build123d Shape/Part."""
    translation = _finite_vector("translation", translation)
    if is_identity(rotation, translation):
        return shape
    # OCP gp_GTrsf constructed from explicit matrix values can be marked
    # gp_Other and rejected by Trsf() even when the matrix is orthonormal.
    # build123d's rotate()/translate() use gp_Trsf directly and remain rigid.
    from build123d import Axis, Vector

    angle, axis = _rotation_to_axis_angle(rotation)
    if abs(angle) > 1e-10:
        shape = shape.rotate(Axis((0.0, 0.0, 0.0), axis), angle)
    if any(component != 0.0 for component in translation):
        shape = shape.translate(Vector(*translation))
    return shape


__all__ = [
    "apply_rigid_transform",
    "axis_angle_to_matrix",
    "is_identity",
    "orientation_from_axis",
    "validate_rotation_matrix",
]
