"""BRep topology selection queries.

The 3D frontend sees an STL mesh, but industrial CAD selection must be answered
from the authoritative BRep geometry.  This module maps a clicked world point to
an OCC face, disambiguates coincident/visible faces with the camera ray, and
returns stable-enough topology data plus a display mesh for face-level highlight.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomAbs import GeomAbs_BSplineCurve, GeomAbs_BezierCurve, GeomAbs_Circle, GeomAbs_Cylinder, GeomAbs_Ellipse, GeomAbs_Line, GeomAbs_Plane
from OCP.gp import gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_Orientation, TopAbs_VERTEX
from OCP.TopLoc import TopLoc_Location
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Vertex

FaceHit = tuple[TopoDS_Face, float, tuple[float, float, float], int]
MAX_DISPLAY_TRIANGLES = 50_000


def _shape(geometry: Any):
    return getattr(geometry, "wrapped", geometry)


def iter_faces(geometry: Any) -> Iterable[TopoDS_Face]:
    explorer = TopExp_Explorer(_shape(geometry), TopAbs_FACE)
    while explorer.More():
        yield TopoDS.Face_s(explorer.Current())
        explorer.Next()


def iter_edges(geometry: Any) -> Iterable[TopoDS_Edge]:
    explorer = TopExp_Explorer(_shape(geometry), TopAbs_EDGE)
    while explorer.More():
        yield TopoDS.Edge_s(explorer.Current())
        explorer.Next()


def iter_vertices(geometry: Any) -> Iterable[TopoDS_Vertex]:
    explorer = TopExp_Explorer(_shape(geometry), TopAbs_VERTEX)
    while explorer.More():
        yield TopoDS.Vertex_s(explorer.Current())
        explorer.Next()


def _vec(values: Any) -> list[float]:
    return [float(values.X()), float(values.Y()), float(values.Z())]


def _round(values: Iterable[float], digits: int = 6) -> list[float]:
    return [round(float(value), digits) for value in values]


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = repr(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()[:24]


def _surface_data(face: TopoDS_Face) -> dict[str, Any]:
    adaptor = BRepAdaptor_Surface(face)
    surface_type = adaptor.GetType()
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area = float(props.Mass())

    if surface_type == GeomAbs_Plane:
        plane = adaptor.Plane()
        location = plane.Location()
        axis = plane.Axis().Direction()
        return {
            "kind": "plane",
            "origin": _vec(location),
            "normal": _vec(axis),
            "area_mm2": area,
        }
    if surface_type == GeomAbs_Cylinder:
        cylinder = adaptor.Cylinder()
        location = cylinder.Location()
        axis = cylinder.Axis().Direction()
        return {
            "kind": "cylinder",
            "origin": _vec(location),
            "axis": _vec(axis),
            "radius_mm": float(cylinder.Radius()),
            "area_mm2": area,
        }

    # Keep the contract forward-compatible.  Unknown analytic surfaces are still
    # selectable as BRep faces; the UI must not infer dimensions from them.
    return {
        "kind": "other",
        "area_mm2": area,
    }


def _face_distance(
    face: TopoDS_Face,
    point: tuple[float, float, float],
) -> tuple[float, tuple[float, float, float]] | None:
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    distance_engine = BRepExtrema_DistShapeShape(vertex, face)
    if not distance_engine.IsDone():
        return None
    distance = float(distance_engine.Value())
    if not math.isfinite(distance):
        return None
    on_face = distance_engine.PointOnShape2(1)
    return distance, (on_face.X(), on_face.Y(), on_face.Z())


def _shape_distance(
    geometry: Any,
    point: tuple[float, float, float],
) -> float | None:
    """Fast whole-shape rejection before the exhaustive candidate query."""
    shape = _shape(geometry)
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    distance_engine = BRepExtrema_DistShapeShape(vertex, shape)
    if not distance_engine.IsDone():
        return None
    distance = float(distance_engine.Value())
    return distance if math.isfinite(distance) else None


def _face_candidates(
    geometry: Any,
    point: tuple[float, float, float],
    tolerance_mm: float,
) -> list[FaceHit]:
    """Return all faces within tolerance, preserving deterministic face order.

    A click on a face edge/seam can be equally close to several faces.  A single
    whole-shape extrema call reports one support only, so semantic selection must
    retain all nearby candidates before applying camera-ray disambiguation.
    """
    tolerance = max(float(tolerance_mm), 1e-6)
    shape_distance = _shape_distance(geometry, point)
    if shape_distance is None or shape_distance > tolerance:
        return []
    hits: list[FaceHit] = []
    for index, face in enumerate(iter_faces(geometry)):
        result = _face_distance(face, point)
        if result is None:
            continue
        distance, closest_point = result
        if distance <= tolerance:
            hits.append((face, distance, closest_point, index))
    return hits


def _face_normal_at(
    face: TopoDS_Face,
    point: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Return the oriented BRep face normal closest to ``point``."""
    try:
        surface = BRep_Tool.Surface_s(face)
        projection = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
        if not projection.IsDone() or projection.NbPoints() < 1:
            return None
        u, v = projection.LowerDistanceParameters()
        point_on_surface = gp_Pnt()
        normal = gp_Vec()
        BRepGProp_Face(face).Normal(u, v, point_on_surface, normal)
        length = math.sqrt(normal.X() ** 2 + normal.Y() ** 2 + normal.Z() ** 2)
        if not math.isfinite(length) or length <= 1e-9:
            return None
        return (normal.X() / length, normal.Y() / length, normal.Z() / length)
    except Exception:
        # Normal extraction is an optimization for ray disambiguation.  A failure
        # must not prevent selection; the deterministic nearest-face fallback is
        # still valid.
        return None


def _normalized_direction(
    direction: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    if not direction:
        return None
    length = math.sqrt(sum(float(value) ** 2 for value in direction))
    if not math.isfinite(length) or length <= 1e-9:
        return None
    return (float(direction[0]) / length, float(direction[1]) / length, float(direction[2]) / length)


def _choose_face(
    candidates: list[FaceHit],
    direction: tuple[float, float, float] | None,
) -> tuple[FaceHit, tuple[float, float, float] | None]:
    """Choose the visible face when a camera ray is available.

    The frontend ray direction points from the camera into the model.  A visible
    front face therefore has ``dot(face_normal, ray_direction) < 0``.  If no
    candidate is front-facing (grazing/tangent clicks or unknown surfaces), the
    nearest BRep face is retained as a deterministic fallback.
    """
    ray = _normalized_direction(direction)
    if ray is None:
        return candidates[0], None

    front_facing: list[tuple[FaceHit, tuple[float, float, float]]] = []
    for candidate in candidates:
        normal = _face_normal_at(candidate[0], candidate[2])
        if normal is None:
            continue
        dot = normal[0] * ray[0] + normal[1] * ray[1] + normal[2] * ray[2]
        if dot < -1e-6:
            front_facing.append((candidate, normal))
    if front_facing:
        chosen, normal = min(front_facing, key=lambda item: (item[0][1], item[0][3]))
        return chosen, normal
    return candidates[0], _face_normal_at(candidates[0][0], candidates[0][2])


def _same_face_exists(geometry: Any, face: TopoDS_Face) -> bool:
    return any(face.IsSame(candidate) for candidate in iter_faces(geometry))


def _owning_feature(
    feature_geometries: dict[str, Any],
    face: TopoDS_Face,
) -> str | None:
    # Feature geometries are cumulative snapshots.  Return the newest matching
    # feature so the UI lands on the feature that owns the current face, rather
    # than an earlier base feature which happens to contain the same topology.
    matches = [
        feature_id
        for feature_id, geometry in feature_geometries.items()
        if geometry is not None and _same_face_exists(geometry, face)
    ]
    return matches[-1] if matches else None


def _display_deflection(geometry: Any) -> float:
    """Scale face-highlight tessellation to the part without flooding the UI."""
    try:
        diagonal = float(geometry.bounding_box().diagonal)
        if math.isfinite(diagonal) and diagonal > 0:
            return min(0.1, max(0.005, diagonal * 0.001))
    except Exception:
        pass
    return 0.02


def _face_display_mesh(
    geometry: Any,
    face: TopoDS_Face,
) -> dict[str, Any] | None:
    """Return a compact triangle mesh for exactly one selected BRep face."""
    try:
        BRepMesh_IncrementalMesh(face, _display_deflection(geometry), False, 0.35, True)
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            return None
        triangle_count = int(triangulation.NbTriangles())
        if triangle_count < 1 or triangle_count > MAX_DISPLAY_TRIANGLES:
            return None

        transform = location.Transformation()
        vertices: list[list[float]] = []
        for index in range(1, int(triangulation.NbNodes()) + 1):
            point = triangulation.Node(index).Transformed(transform)
            vertices.append([float(point.X()), float(point.Y()), float(point.Z())])

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        indices: list[int] = []
        for index in range(1, triangle_count + 1):
            triangle = triangulation.Triangle(index)
            a = int(triangle.Value(1)) - 1
            b = int(triangle.Value(2)) - 1
            c = int(triangle.Value(3)) - 1
            if reversed_face:
                b, c = c, b
            indices.extend((a, b, c))

        return {
            "type": "triangles",
            "vertices": vertices,
            "indices": indices,
            "triangle_count": triangle_count,
            "source": "brep",
        }
    except Exception:
        # Highlight is optional.  Semantic data remains authoritative even if a
        # display tessellation cannot be produced for this face.
        return None


def _face_selection_payload(
    geometry: Any,
    face: TopoDS_Face,
    distance: float,
    closest_point: tuple[float, float, float],
    point: tuple[float, float, float],
    direction: tuple[float, float, float] | None,
    face_normal: tuple[float, float, float] | None,
    feature_geometries: dict[str, Any],
) -> dict[str, Any]:
    surface = _surface_data(face)
    feature_id = _owning_feature(feature_geometries, face)
    fingerprint_payload = {
        "kind": surface["kind"],
        "origin": _round(surface.get("origin", [])),
        "normal": _round(surface.get("normal", [])),
        "axis": _round(surface.get("axis", [])),
        "radius": round(surface.get("radius_mm", 0.0), 6),
        "area": round(surface.get("area_mm2", 0.0), 6),
    }

    fingerprint = _fingerprint(fingerprint_payload)
    # Do not expose the face enumeration index as a topology ID: rebuilds can
    # reorder faces.  The semantic fingerprint is stable for the same analytic
    # surface and changes when the surface/trim changes.
    return {
        "topology": {
            "type": "face",
            "id": f"face:{fingerprint}",
            "kind": surface["kind"],
            "fingerprint": fingerprint,
        },
        "geometry": surface,
        "hit": {
            "point_mm": [float(value) for value in point],
            "point_on_face_mm": [float(value) for value in closest_point],
            "distance_to_face_mm": distance,
            "ray_direction": [float(value) for value in direction] if direction else None,
            "face_normal": list(face_normal) if face_normal is not None else None,
        },
        "feature_id": feature_id,
        "display": _face_display_mesh(geometry, face),
        "source": "brep",
        "units": "mm",
        "accuracy": 0.001,
    }


def select_face_at_point(
    geometry: Any,
    point: tuple[float, float, float],
    *,
    feature_geometries: dict[str, Any] | None = None,
    tolerance_mm: float = 0.2,
    direction: tuple[float, float, float] | None = None,
) -> dict[str, Any] | None:
    """Resolve a mesh hit to one semantic BRep face.

    ``direction`` is the camera ray from the eye through the mesh hit.  It is
    used to prefer the face the user actually saw when an edge/seam is close to
    several BRep faces.
    """
    candidates = _face_candidates(geometry, point, tolerance_mm)
    if not candidates:
        return None

    (face, distance, closest_point, _face_order), face_normal = _choose_face(candidates, direction)
    return _face_selection_payload(
        geometry,
        face,
        distance,
        closest_point,
        point,
        direction,
        face_normal,
        feature_geometries or {},
    )


def _resolve_topology_by_id(
    geometry: Any,
    topology_id: str,
    *,
    feature_geometries: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]] | None:
    """Resolve a public semantic ID to its current OCC shape and payload."""
    prefix = topology_id.split(":", 1)[0]
    features = feature_geometries or {}

    if prefix == "face":
        for face in iter_faces(geometry):
            surface = _surface_data(face)
            anchor = tuple(surface.get("origin", (0.0, 0.0, 0.0)))
            payload = _face_selection_payload(
                geometry,
                face,
                0.0,
                anchor,
                anchor,
                None,
                None,
                features,
            )
            if payload["topology"]["id"] == topology_id:
                payload["hit"] = None
                return face, payload
        return None

    if prefix == "edge":
        for edge in iter_edges(geometry):
            data = _edge_data(edge)
            anchor = tuple(data.get("start_point", (0.0, 0.0, 0.0)))
            payload = _edge_selection(
                geometry,
                edge,
                0.0,
                anchor,
                anchor,
                None,
                features,
            )
            if payload["topology"]["id"] == topology_id:
                payload["hit"] = None
                return edge, payload
        return None

    for vertex in iter_vertices(geometry):
        data = _vertex_data(vertex)
        anchor = tuple(data["point"])
        payload = _vertex_selection(
            geometry,
            vertex,
            0.0,
            anchor,
            None,
            features,
        )
        if payload["topology"]["id"] == topology_id:
            payload["hit"] = None
            return vertex, payload
    return None


def query_topology_by_id(
    geometry: Any,
    topology_id: str,
    *,
    feature_geometries: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve a semantic topology ID against the current BRep.

    This reverse lookup is the base for measurement, drawing, and parameter-edit
    references.  It intentionally does not accept array indexes; the public ID is
    the semantic fingerprint emitted by selection.
    """
    if not isinstance(topology_id, str):
        raise ValueError("topology_id must be a string")
    prefix = topology_id.split(":", 1)[0]
    if prefix not in ("face", "edge", "vertex"):
        raise ValueError("topology_id must start with face:, edge:, or vertex:")
    resolved = _resolve_topology_by_id(
        geometry,
        topology_id,
        feature_geometries=feature_geometries,
    )
    return resolved[1] if resolved is not None else None


def _axis_unit(values: Iterable[float]) -> tuple[float, float, float] | None:
    axis = tuple(float(value) for value in values)
    if len(axis) != 3:
        return None
    length = math.sqrt(sum(value * value for value in axis))
    if not math.isfinite(length) or length <= 1e-9:
        return None
    return (axis[0] / length, axis[1] / length, axis[2] / length)


def _diameter_measure(
    payload: dict[str, Any],
    topology_id: str,
) -> dict[str, Any] | None:
    geometry = payload.get("geometry", {})
    if geometry.get("kind") not in ("cylinder", "circle"):
        return None
    radius = geometry.get("radius_mm")
    center = geometry.get("origin", geometry.get("center"))
    axis = _axis_unit(geometry.get("axis", (0.0, 0.0, 1.0)))
    if (
        not isinstance(radius, (int, float))
        or not math.isfinite(float(radius))
        or float(radius) <= 0.0
        or not isinstance(center, (list, tuple))
        or len(center) != 3
        or axis is None
    ):
        return None

    # Any vector perpendicular to the axis gives a valid diameter chord on the
    # analytic circle/cylinder.  Pick the least-parallel world axis for stability.
    helper = min(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        key=lambda candidate: abs(sum(a * b for a, b in zip(axis, candidate))),
    )
    radial = (
        axis[1] * helper[2] - axis[2] * helper[1],
        axis[2] * helper[0] - axis[0] * helper[2],
        axis[0] * helper[1] - axis[1] * helper[0],
    )
    radial_length = math.sqrt(sum(value * value for value in radial))
    if radial_length <= 1e-9:
        return None
    radial = tuple(value / radial_length for value in radial)
    radius = float(radius)
    center = tuple(float(value) for value in center)
    p1 = tuple(center[index] - radius * radial[index] for index in range(3))
    p2 = tuple(center[index] + radius * radial[index] for index in range(3))
    return _measure_payload(
        topology_ids=[topology_id],
        metric="diameter",
        p1=p1,
        p2=p2,
        distance=radius * 2.0,
        algorithm="occ_analytic_brep",
    )


def _axis_to_axis_measure(
    first: dict[str, Any],
    second: dict[str, Any],
    topology_ids: list[str],
) -> dict[str, Any] | None:
    """Return the analytic distance between two cylinder/circle axes.

    Hole center distance is an industrial measurement in its own right; using
    the surface minimum distance would subtract/add radii and mislead the user.
    """
    lines: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for payload in (first, second):
        geometry = payload.get("geometry", {})
        if geometry.get("kind") not in ("cylinder", "circle"):
            return None
        origin = geometry.get("origin", geometry.get("center"))
        axis = _axis_unit(geometry.get("axis"))
        if (
            not isinstance(origin, (list, tuple))
            or len(origin) != 3
            or axis is None
            or not all(math.isfinite(float(value)) for value in origin)
        ):
            return None
        lines.append((tuple(float(value) for value in origin), axis))

    (p1, d1), (p2, d2) = lines
    cross = (
        d1[1] * d2[2] - d1[2] * d2[1],
        d1[2] * d2[0] - d1[0] * d2[2],
        d1[0] * d2[1] - d1[1] * d2[0],
    )
    cross_length = math.sqrt(sum(value * value for value in cross))
    w = tuple(p1[index] - p2[index] for index in range(3))

    if cross_length <= 1e-9:
        # Parallel (or coincident) axes: project p2 onto line 1, then measure the
        # perpendicular offset.  The sign matters when the two origins are at
        # different positions along the same axis direction.
        projection = sum(-w[index] * d1[index] for index in range(3))
        q1 = tuple(p1[index] + projection * d1[index] for index in range(3))
        q2 = p2
        offset = tuple(q2[index] - q1[index] for index in range(3))
        distance = math.sqrt(sum(value * value for value in offset))
    else:
        # General skew/intersecting lines: solve for the closest point pair.
        # Projecting each origin onto the *other* axis is only valid when the
        # axes are orthogonal; non-orthogonal axes need the coupled solution.
        c = tuple(p2[index] - p1[index] for index in range(3))
        axis_alignment = sum(d1[index] * d2[index] for index in range(3))
        denominator = 1.0 - axis_alignment * axis_alignment
        if abs(denominator) <= 1e-12:
            return None
        d1c = sum(d1[index] * c[index] for index in range(3))
        d2c = sum(d2[index] * c[index] for index in range(3))
        t1 = (d1c - axis_alignment * d2c) / denominator
        t2 = t1 * axis_alignment - d2c
        q1 = tuple(p1[index] + t1 * d1[index] for index in range(3))
        q2 = tuple(p2[index] + t2 * d2[index] for index in range(3))
        distance = abs(sum(cross[index] * w[index] for index in range(3))) / cross_length
        if distance <= 1e-9:
            # Intersecting axes: avoid a numerical near-zero evidence segment.
            q2 = q1

    if not math.isfinite(distance):
        return None
    return _measure_payload(
        topology_ids=topology_ids,
        metric="axis_to_axis",
        p1=q1,
        p2=q2,
        distance=distance,
        algorithm="occ_analytic_brep",
    )


def _parallel_plane_measure(
    first: dict[str, Any],
    second: dict[str, Any],
    topology_ids: list[str],
) -> dict[str, Any] | None:
    """Return the perpendicular distance between two parallel BRep planes."""
    planes: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for payload in (first, second):
        geometry = payload.get("geometry", {})
        if geometry.get("kind") != "plane":
            return None
        origin = geometry.get("origin")
        normal = _axis_unit(geometry.get("normal"))
        if (
            not isinstance(origin, (list, tuple))
            or len(origin) != 3
            or normal is None
            or not all(math.isfinite(float(value)) for value in origin)
        ):
            return None
        planes.append((tuple(float(value) for value in origin), normal))

    (p1, n1), (p2, n2) = planes
    normal_alignment = abs(sum(n1[index] * n2[index] for index in range(3)))
    if abs(normal_alignment - 1.0) > 1e-7:
        return None

    offset = tuple(p2[index] - p1[index] for index in range(3))
    signed_distance = sum(offset[index] * n1[index] for index in range(3))
    if not math.isfinite(signed_distance):
        return None
    if abs(signed_distance) <= 1e-12:
        q2 = p1
    else:
        q2 = tuple(p1[index] + signed_distance * n1[index] for index in range(3))
    return _measure_payload(
        topology_ids=topology_ids,
        metric="face_to_face",
        p1=p1,
        p2=q2,
        distance=abs(signed_distance),
        algorithm="occ_analytic_brep",
    )


def _measure_payload(
    *,
    topology_ids: list[str],
    metric: str,
    p1: tuple[float, float, float],
    p2: tuple[float, float, float],
    distance: float,
    algorithm: str,
) -> dict[str, Any]:
    return {
        "topology_ids": topology_ids,
        "metric": metric,
        "result": {
            "p1": [float(value) for value in p1],
            "p2": [float(value) for value in p2],
            "distance": float(distance),
            "dx": abs(float(p2[0]) - float(p1[0])),
            "dy": abs(float(p2[1]) - float(p1[1])),
            "dz": abs(float(p2[2]) - float(p1[2])),
        },
        "algorithm": algorithm,
        "source": "brep",
        "units": "mm",
        "accuracy": 0.001,
    }


def measure_topology(
    geometry: Any,
    topology_ids: list[str] | tuple[str, ...],
    *,
    feature_geometries: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Measure one or two semantic BRep topology references.

    Current M2 scope: one cylinder/circle reference returns diameter; two
    cylinder/circle references return axis-to-axis distance; two parallel plane
    faces return face-to-face distance; any other pair returns the OCC minimum
    distance.  All results carry evidence points and an explicit BRep algorithm
    so the frontend never presents a mesh approximation as an engineering
    measurement.
    """
    ids = list(topology_ids)
    if not ids or len(ids) > 2 or not all(isinstance(item, str) for item in ids):
        raise ValueError("topology_ids must contain one or two semantic topology IDs")

    resolved: list[tuple[Any, dict[str, Any]]] = []
    for topology_id in ids:
        item = _resolve_topology_by_id(
            geometry,
            topology_id,
            feature_geometries=feature_geometries,
        )
        if item is None:
            return None
        resolved.append(item)

    if len(resolved) == 1:
        diameter = _diameter_measure(resolved[0][1], ids[0])
        if diameter is None:
            raise ValueError("single-reference BRep measurement currently supports cylinder or circle diameter")
        return diameter

    axis_to_axis = _axis_to_axis_measure(resolved[0][1], resolved[1][1], ids)
    if axis_to_axis is not None:
        return axis_to_axis

    face_to_face = _parallel_plane_measure(resolved[0][1], resolved[1][1], ids)
    if face_to_face is not None:
        return face_to_face

    engine = BRepExtrema_DistShapeShape(_shape(resolved[0][0]), _shape(resolved[1][0]))
    if not engine.IsDone():
        raise RuntimeError("OCC BRep distance query failed")
    distance = float(engine.Value())
    if not math.isfinite(distance):
        raise RuntimeError("OCC BRep distance is not finite")
    point1 = engine.PointOnShape1(1)
    point2 = engine.PointOnShape2(1)
    return _measure_payload(
        topology_ids=ids,
        metric="minimum_distance",
        p1=(point1.X(), point1.Y(), point1.Z()),
        p2=(point2.X(), point2.Y(), point2.Z()),
        distance=distance,
        algorithm="occ_brep_extrema",
    )


def _shape_exists(
    geometry: Any,
    shape: Any,
    shape_type: int,
) -> bool:
    explorer = TopExp_Explorer(_shape(geometry), shape_type)
    while explorer.More():
        if shape.IsSame(explorer.Current()):
            return True
        explorer.Next()
    return False


def _owning_shape_feature(
    feature_geometries: dict[str, Any],
    shape: Any,
    shape_type: int,
) -> str | None:
    matches = [
        feature_id
        for feature_id, geometry in feature_geometries.items()
        if geometry is not None and _shape_exists(geometry, shape, shape_type)
    ]
    return matches[-1] if matches else None


def _edge_data(edge: TopoDS_Edge) -> dict[str, Any]:
    adaptor = BRepAdaptor_Curve(edge)
    curve_type = adaptor.GetType()
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    length = float(props.Mass())
    first = adaptor.FirstParameter()
    last = adaptor.LastParameter()
    start = adaptor.Value(first)
    end = adaptor.Value(last)
    data: dict[str, Any] = {
        "kind": "other",
        "length_mm": length,
        "start_point": _vec(start),
        "end_point": _vec(end),
    }

    if curve_type == GeomAbs_Line:
        line = adaptor.Line()
        data["kind"] = "line"
        data["direction"] = _vec(line.Direction())
    elif curve_type == GeomAbs_Circle:
        circle = adaptor.Circle()
        data["kind"] = "circle"
        data["center"] = _vec(circle.Location())
        data["axis"] = _vec(circle.Axis().Direction())
        data["radius_mm"] = float(circle.Radius())
    elif curve_type == GeomAbs_Ellipse:
        ellipse = adaptor.Ellipse()
        data["kind"] = "ellipse"
        data["center"] = _vec(ellipse.Location())
        data["major_radius_mm"] = float(ellipse.MajorRadius())
        data["minor_radius_mm"] = float(ellipse.MinorRadius())
    elif curve_type in (GeomAbs_BSplineCurve, GeomAbs_BezierCurve):
        data["kind"] = "spline"
    return data


def _vertex_data(vertex: TopoDS_Vertex) -> dict[str, Any]:
    point = BRep_Tool.Pnt_s(vertex)
    return {
        "kind": "point",
        "point": _vec(point),
    }


def _edge_display(edge: TopoDS_Edge) -> dict[str, Any] | None:
    """Sample the exact BRep edge for a highlight polyline."""
    try:
        adaptor = BRepAdaptor_Curve(edge)
        first = float(adaptor.FirstParameter())
        last = float(adaptor.LastParameter())
        if not math.isfinite(first) or not math.isfinite(last) or last <= first:
            return None
        segments = 1 if adaptor.GetType() == GeomAbs_Line else 64
        vertices: list[list[float]] = []
        for index in range(segments + 1):
            parameter = first + (last - first) * index / segments
            point = adaptor.Value(parameter)
            vertices.append([float(point.X()), float(point.Y()), float(point.Z())])
        if len(vertices) < 2:
            return None
        return {
            "type": "polyline",
            "vertices": vertices,
            "source": "brep",
        }
    except Exception:
        return None


def _vertex_display(vertex: TopoDS_Vertex) -> dict[str, Any] | None:
    try:
        point = BRep_Tool.Pnt_s(vertex)
        return {
            "type": "point",
            "point": _vec(point),
            "source": "brep",
        }
    except Exception:
        return None


def _vertex_selection(
    geometry: Any,
    vertex: TopoDS_Vertex,
    distance: float,
    point: tuple[float, float, float],
    direction: tuple[float, float, float] | None,
    feature_geometries: dict[str, Any],
) -> dict[str, Any]:
    data = _vertex_data(vertex)
    fingerprint_payload = {
        "kind": data["kind"],
        "point": _round(data["point"]),
    }
    fingerprint = _fingerprint(fingerprint_payload)
    return {
        "topology": {
            "type": "vertex",
            "id": f"vertex:{fingerprint}",
            "kind": data["kind"],
            "fingerprint": fingerprint,
        },
        "geometry": data,
        "hit": {
            "point_mm": [float(value) for value in point],
            "point_on_vertex_mm": data["point"],
            "distance_to_vertex_mm": distance,
            "ray_direction": [float(value) for value in direction] if direction else None,
        },
        "feature_id": _owning_shape_feature(feature_geometries, vertex, TopAbs_VERTEX),
        "display": _vertex_display(vertex),
        "source": "brep",
        "units": "mm",
        "accuracy": 0.001,
    }


def _edge_selection(
    geometry: Any,
    edge: TopoDS_Edge,
    distance: float,
    closest_point: tuple[float, float, float],
    point: tuple[float, float, float],
    direction: tuple[float, float, float] | None,
    feature_geometries: dict[str, Any],
) -> dict[str, Any]:
    data = _edge_data(edge)
    fingerprint_payload = {
        "kind": data["kind"],
        "length": round(float(data.get("length_mm", 0.0)), 6),
        "start": _round(data.get("start_point", [])),
        "end": _round(data.get("end_point", [])),
        "center": _round(data.get("center", [])),
        "axis": _round(data.get("axis", [])),
        "radius": round(float(data.get("radius_mm", 0.0)), 6),
    }
    fingerprint = _fingerprint(fingerprint_payload)
    return {
        "topology": {
            "type": "edge",
            "id": f"edge:{fingerprint}",
            "kind": data["kind"],
            "fingerprint": fingerprint,
        },
        "geometry": data,
        "hit": {
            "point_mm": [float(value) for value in point],
            "point_on_edge_mm": [float(value) for value in closest_point],
            "distance_to_edge_mm": distance,
            "ray_direction": [float(value) for value in direction] if direction else None,
        },
        "feature_id": _owning_shape_feature(feature_geometries, edge, TopAbs_EDGE),
        "display": _edge_display(edge),
        "source": "brep",
        "units": "mm",
        "accuracy": 0.001,
    }


def select_topology_at_point(
    geometry: Any,
    point: tuple[float, float, float],
    *,
    feature_geometries: dict[str, Any] | None = None,
    tolerance_mm: float = 0.2,
    direction: tuple[float, float, float] | None = None,
) -> dict[str, Any] | None:
    """Resolve a mesh hit to a BRep vertex, edge, or face.

    OCC's whole-shape nearest query already reports whether the nearest topology
    is a vertex, edge, or face.  Using that support avoids a full edge/vertex
    scan on every click and keeps the selection deterministic.  The mesh hit is
    still only a locator; all returned dimensions come from the BRep.
    """
    tolerance = max(float(tolerance_mm), 1e-6)
    shape = _shape(geometry)
    locator = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    distance_engine = BRepExtrema_DistShapeShape(locator, shape)
    if not distance_engine.IsDone():
        return None
    distance = float(distance_engine.Value())
    if not math.isfinite(distance) or distance > tolerance:
        return None

    features = feature_geometries or {}
    support = distance_engine.SupportOnShape2(1)
    on_shape = distance_engine.PointOnShape2(1)
    closest_point = (on_shape.X(), on_shape.Y(), on_shape.Z())
    if not support.IsNull() and support.ShapeType() == TopAbs_VERTEX:
        vertex = TopoDS.Vertex_s(support)
        return _vertex_selection(geometry, vertex, distance, point, direction, features)
    if not support.IsNull() and support.ShapeType() == TopAbs_EDGE:
        edge = TopoDS.Edge_s(support)
        return _edge_selection(
            geometry, edge, distance, closest_point, point, direction, features
        )
    return select_face_at_point(
        geometry,
        point,
        feature_geometries=feature_geometries,
        tolerance_mm=tolerance_mm,
        direction=direction,
    )
