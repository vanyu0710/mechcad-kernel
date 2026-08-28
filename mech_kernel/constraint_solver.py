"""Deterministic 2-D sketch constraint solving for the v2.4 kernel."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Tuple

from .errors import InvalidRequestError
from .features import ConstraintStatus, Sketch

SUPPORTED_CONSTRAINTS = frozenset({
    "coincident", "horizontal", "vertical", "parallel",
    "perpendicular", "distance", "radius", "equal",
})
POINT_NAMES = frozenset({"start", "end", "center"})
SOLVE_TOLERANCE = 1e-6


@dataclass
class SolveResult:
    status: ConstraintStatus
    dof: int
    residual: float
    conflicting_constraints: List[str]
    under_constrained_entities: List[str]
    iterations: int

    def to_dict(self, sketch_name: str, constraint_count: int) -> dict:
        return {
            "sketch": sketch_name,
            "status": self.status.value,
            "dof": self.dof,
            "constraint_count": constraint_count,
            "residual": self.residual,
            "conflicting_constraints": list(self.conflicting_constraints),
            "under_constrained_entities": list(self.under_constrained_entities),
            "solver_iterations": self.iterations,
        }


def validate_constraint(constraint_type: str, references: list, value: Any = None) -> None:
    if constraint_type not in SUPPORTED_CONSTRAINTS:
        raise InvalidRequestError(f"constraint_type 必须是 {sorted(SUPPORTED_CONSTRAINTS)}")
    if not isinstance(references, list):
        raise InvalidRequestError("references 必须是列表")
    expected = {
        "coincident": 2, "horizontal": 1, "vertical": 1,
        "parallel": 2, "perpendicular": 2, "distance": 2,
        "radius": 1, "equal": 2,
    }[constraint_type]
    if len(references) != expected:
        raise InvalidRequestError(f"{constraint_type} 需要 {expected} 个引用")
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get("entity_id"), str):
            raise InvalidRequestError("每个引用必须包含 entity_id 字符串")
        if "point" not in ref and "role" not in ref:
            raise InvalidRequestError("引用必须包含 point 或 role")
        if "point" in ref and ref["point"] not in POINT_NAMES:
            raise InvalidRequestError("point 必须是 start/end/center")
        if "role" in ref and ref["role"] not in ("line", "circle"):
            raise InvalidRequestError("role 必须是 line/circle")
    if constraint_type in ("distance", "radius"):
        if value is None or isinstance(value, bool):
            raise InvalidRequestError(f"{constraint_type} 需要 value")
        try:
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise InvalidRequestError(f"{constraint_type}.value 必须是正数")


def _point(entity: Any, point_name: str) -> Tuple[float, float]:
    if entity.type == "line":
        if point_name not in ("start", "end"):
            raise InvalidRequestError("line 只支持 start/end 点")
        return tuple(float(v) for v in entity.params[point_name])
    if entity.type == "circle" and point_name == "center":
        return tuple(float(v) for v in entity.params["center"])
    raise InvalidRequestError(f"实体 {entity.id} 不支持点引用 {point_name}")


def _set_point(entity: Any, point_name: str, value: Tuple[float, float]) -> None:
    if entity.type == "line" and point_name in ("start", "end"):
        entity.params[point_name] = tuple(value)
    elif entity.type == "circle" and point_name == "center":
        entity.params["center"] = tuple(value)
    else:
        raise InvalidRequestError(f"实体 {entity.id} 不支持点引用 {point_name}")


def _line_vector(entity: Any) -> Tuple[float, float]:
    start = _point(entity, "start")
    end = _point(entity, "end")
    return end[0] - start[0], end[1] - start[1]


def _entity_ref(ref: dict, entities: Dict[str, Any]) -> Any:
    entity = entities.get(ref["entity_id"])
    if entity is None:
        raise InvalidRequestError(f"实体不存在: {ref['entity_id']}")
    return entity


def _residual_blocks(sketch: Sketch, values: Dict[Tuple[str, str, int], float]) -> Tuple[List[float], List[str], List[str]]:
    entities = {e.id: e for e in sketch.entities}
    working = {e.id: type("Entity", (), {"id": e.id, "type": e.type, "params": dict(e.params)})() for e in sketch.entities}
    for entity in working.values():
        if entity.type == "line":
            _set_point(entity, "start", (values[(entity.id, "start", 0)], values[(entity.id, "start", 1)]))
            _set_point(entity, "end", (values[(entity.id, "end", 0)], values[(entity.id, "end", 1)]))
        elif entity.type == "circle":
            _set_point(entity, "center", (values[(entity.id, "center", 0)], values[(entity.id, "center", 1)]))
            entity.params["radius"] = values[(entity.id, "radius", 0)]

    residuals: List[float] = []
    labels: List[str] = []
    owners: List[str] = []
    for constraint in sorted(sketch.constraints, key=lambda c: c.id):
        refs = constraint.references
        ctype = constraint.type
        block: List[float]
        if ctype == "coincident":
            p1 = _point(_entity_ref(refs[0], working), refs[0].get("point", "center"))
            p2 = _point(_entity_ref(refs[1], working), refs[1].get("point", "center"))
            block = [p1[0] - p2[0], p1[1] - p2[1]]
        elif ctype in ("horizontal", "vertical"):
            entity = _entity_ref(refs[0], working)
            if entity.type != "line":
                raise InvalidRequestError(f"{ctype} 只支持 line")
            dx, dy = _line_vector(entity)
            block = [dy if ctype == "horizontal" else dx]
        elif ctype in ("parallel", "perpendicular"):
            e1, e2 = _entity_ref(refs[0], working), _entity_ref(refs[1], working)
            if e1.type != "line" or e2.type != "line":
                raise InvalidRequestError(f"{ctype} 只支持两个 line")
            ax, ay = _line_vector(e1)
            bx, by = _line_vector(e2)
            block = [ax * by - ay * bx if ctype == "parallel" else ax * bx + ay * by]
        elif ctype == "distance":
            p1 = _point(_entity_ref(refs[0], working), refs[0].get("point", "center"))
            p2 = _point(_entity_ref(refs[1], working), refs[1].get("point", "center"))
            block = [math.hypot(p1[0] - p2[0], p1[1] - p2[1]) - float(constraint.value)]
        elif ctype == "radius":
            entity = _entity_ref(refs[0], working)
            if entity.type != "circle":
                raise InvalidRequestError("radius 只支持 circle")
            block = [float(entity.params["radius"]) - float(constraint.value)]
        else:  # equal
            e1, e2 = _entity_ref(refs[0], working), _entity_ref(refs[1], working)
            if e1.type == e2.type == "line":
                block = [math.hypot(*_line_vector(e1)) - math.hypot(*_line_vector(e2))]
            elif e1.type == e2.type == "circle":
                block = [float(e1.params["radius"]) - float(e2.params["radius"])]
            else:
                raise InvalidRequestError("equal 需要两个同类型 line 或 circle")
        residuals.extend(block)
        labels.extend([constraint.id] * len(block))
        owners.extend([ref["entity_id"] for ref in refs])
    return residuals, labels, owners


def solve_sketch(sketch: Sketch, parameters: Dict[str, float]) -> SolveResult:
    """Solve in a deterministic variable/constraint order and mutate on success."""
    validate = {e.id: e for e in sketch.entities}
    supported = {"line", "circle"}
    for entity in sketch.entities:
        if entity.type not in supported and sketch.constraints:
            raise InvalidRequestError(f"约束求解暂不支持 entity type={entity.type}")
    for constraint in sketch.constraints:
        if constraint.parameter_name:
            if constraint.parameter_name not in parameters:
                raise InvalidRequestError(f"参数不存在: {constraint.parameter_name}")
            constraint.value = float(parameters[constraint.parameter_name])
        validate_constraint(constraint.type, constraint.references, constraint.value)
        for ref in constraint.references:
            if ref["entity_id"] not in validate:
                raise InvalidRequestError(f"实体不存在: {ref['entity_id']}")

    variables: List[Tuple[str, str, int]] = []
    initial: List[float] = []
    for entity in sorted(sketch.entities, key=lambda e: e.id):
        if entity.type == "line":
            for key in ("start", "end"):
                variables.extend([(entity.id, key, 0), (entity.id, key, 1)])
                initial.extend(float(v) for v in entity.params[key])
        elif entity.type == "circle":
            variables.extend([(entity.id, "center", 0), (entity.id, "center", 1), (entity.id, "radius", 0)])
            initial.extend([float(entity.params["center"][0]), float(entity.params["center"][1]), float(entity.params["radius"])])

    if not sketch.constraints:
        return SolveResult(ConstraintStatus.UNDER_CONSTRAINED, len(variables), 0.0, [], [e.id for e in sketch.entities], 0)

    try:
        import numpy as np
        from scipy.optimize import least_squares
    except ImportError as exc:
        raise InvalidRequestError("约束求解需要 scipy 和 numpy") from exc

    def residual(vector):
        values = dict(zip(variables, vector))
        return _residual_blocks(sketch, values)[0]

    result = least_squares(
        residual, np.asarray(initial, dtype=float), method="trf",
        max_nfev=200, xtol=1e-12, ftol=1e-12, gtol=1e-12,
    )
    values = dict(zip(variables, result.x))
    residual_vector, labels, owners = _residual_blocks(sketch, values)
    max_residual = max((abs(v) for v in residual_vector), default=0.0)
    rank = int(np.linalg.matrix_rank(result.jac)) if result.jac.size else 0
    dof = max(0, len(variables) - rank)
    conflicting = sorted({label for label, value in zip(labels, residual_vector) if abs(value) > SOLVE_TOLERANCE})
    degenerate_entities = set()
    for entity in sketch.entities:
        if entity.type == "line":
            dx = values[(entity.id, "end", 0)] - values[(entity.id, "start", 0)]
            dy = values[(entity.id, "end", 1)] - values[(entity.id, "start", 1)]
            if math.hypot(dx, dy) <= SOLVE_TOLERANCE:
                degenerate_entities.add(entity.id)
    if degenerate_entities:
        conflicting.extend(
            constraint.id for constraint in sketch.constraints
            if any(ref["entity_id"] in degenerate_entities for ref in constraint.references)
        )
        conflicting = sorted(set(conflicting))
        max_residual = max(max_residual, SOLVE_TOLERANCE * 10)
    if max_residual > SOLVE_TOLERANCE:
        status = ConstraintStatus.CONFLICT
    elif dof > 0:
        status = ConstraintStatus.UNDER_CONSTRAINED
    elif len(residual_vector) > rank:
        status = ConstraintStatus.OVER_CONSTRAINED
    else:
        status = ConstraintStatus.SOLVED

    for entity in sketch.entities:
        if entity.type == "line":
            _set_point(entity, "start", (values[(entity.id, "start", 0)], values[(entity.id, "start", 1)]))
            _set_point(entity, "end", (values[(entity.id, "end", 0)], values[(entity.id, "end", 1)]))
        elif entity.type == "circle":
            _set_point(entity, "center", (values[(entity.id, "center", 0)], values[(entity.id, "center", 1)]))
            entity.params["radius"] = float(values[(entity.id, "radius", 0)])
    constrained_ids = {ref["entity_id"] for constraint in sketch.constraints for ref in constraint.references}
    under = sorted({entity_id for entity_id in constrained_ids if dof > 0})
    return SolveResult(status, dof, max_residual, conflicting, under, int(getattr(result, "nfev", 0)))
