"""Compact 2-D sketch evidence rendering for vision models."""
from __future__ import annotations

import io
from typing import Any, Dict


def render_sketch(sketch: Any, size: int = 640, annotate: bool = True) -> Dict[str, bytes]:
    fig = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle
    except ImportError:
        return {}
    if sketch is None or not sketch.entities:
        return {}
    try:
        points = []
        for entity in sketch.entities:
            if entity.type == "line":
                points.extend([entity.params["start"], entity.params["end"]])
            elif entity.type == "circle":
                cx, cy = entity.params["center"]
                r = float(entity.params["radius"])
                points.extend([(cx - r, cy - r), (cx + r, cy + r)])
            elif entity.type == "rectangle":
                cx, cy = entity.params.get("center", (0, 0))
                w, h = entity.params["width"], entity.params["height"]
                points.extend([(cx - w / 2, cy - h / 2), (cx + w / 2, cy + h / 2)])
            elif entity.type in ("polyline",):
                points.extend(entity.params["points"])
        if not points:
            return {}
        xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        pad = span * 0.12
        dpi = 100
        fig, ax = plt.subplots(figsize=(size / dpi, size / dpi), dpi=dpi)
        conflicting_ids = set(getattr(sketch, "conflicting_constraints", []))
        conflicting_entities = {
            ref.get("entity_id")
            for constraint in sketch.constraints
            if constraint.id in conflicting_ids
            for ref in constraint.references
            if isinstance(ref, dict) and ref.get("entity_id")
        }
        for entity in sketch.entities:
            color = "#c0392b" if entity.id in conflicting_entities else "#2f6f9f"
            if entity.type == "line":
                (x1, y1), (x2, y2) = entity.params["start"], entity.params["end"]
                ax.plot([x1, x2], [y1, y2], color=color, linewidth=2.0)
            elif entity.type == "circle":
                cx, cy = entity.params["center"]
                ax.add_patch(Circle((cx, cy), entity.params["radius"], fill=False, color=color, linewidth=2.0))
            elif entity.type == "rectangle":
                cx, cy = entity.params.get("center", (0, 0))
                w, h = entity.params["width"], entity.params["height"]
                ax.add_patch(plt.Rectangle((cx - w / 2, cy - h / 2), w, h, fill=False, color=color, linewidth=2.0))
            elif entity.type == "polyline":
                coords = entity.params["points"]
                ax.plot([p[0] for p in coords], [p[1] for p in coords], color=color, linewidth=2.0)
            if annotate:
                anchor = entity.params.get("center") or entity.params.get("start") or entity.params.get("points", [(0, 0)])[0]
                ax.text(anchor[0], anchor[1], entity.name or entity.id, fontsize=8, color="#1f2933")
        ax.set_xlim(min(xs) - pad, max(xs) + pad)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
        ax.set_aspect("equal")
        ax.axis("off")
        if annotate:
            status = getattr(sketch.solver_status, "value", str(sketch.solver_status))
            ax.text(0.02, 0.98, f"{sketch.name} | {status} | DOF {sketch.dof}", transform=ax.transAxes, va="top", fontsize=9)
            constraints = " ".join(f"{c.id}:{c.type}" for c in sorted(sketch.constraints, key=lambda c: c.id))
            if constraints:
                ax.text(0.02, 0.02, constraints, transform=ax.transAxes, va="bottom", fontsize=7, color="#4b5563")
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        out = io.BytesIO()
        fig.savefig(out, format="png", dpi=dpi, facecolor="white", pad_inches=0)
        plt.close(fig)
        png = out.getvalue()
        return {"sketch": png, "default": png}
    except Exception:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass
        return {}
