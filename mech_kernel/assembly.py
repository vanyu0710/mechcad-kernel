"""Instance-level assembly display state.

The kernel continues to store a fused STEP-compatible solid.  This module keeps
the source-instance metadata needed by renderers without changing replay
semantics or the exported fused geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AssemblyInstance:
    id: str
    name: str
    path: str
    position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: Optional[list] = None
    color: list[float] = field(default_factory=lambda: [0.36, 0.56, 0.76])
    visible: bool = True
    bbox: Optional[list[float]] = None
    geometry: Any = field(default=None, repr=False, compare=False)
    # v2.7: reference-frame fields
    local_origin: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    mount_frame: Optional[str] = None  # frame name in FrameRegistry
    world_transform: Optional[list] = None  # 4x4 flattened row-major
    mount_uv: list[float] = field(default_factory=lambda: [0.0, 0.0])
    mount_normal_offset: float = 0.0

    def to_dict(self, include_geometry: bool = False) -> dict:
        result = {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "position": list(self.position),
            "rotation": self.rotation,
            "color": list(self.color),
            "visible": self.visible,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "local_origin": list(self.local_origin),
            "mount_frame": self.mount_frame,
            "world_transform": self.world_transform,
            "mount_uv": list(self.mount_uv),
            "mount_normal_offset": float(self.mount_normal_offset),
        }
        if include_geometry:
            result["geometry"] = self.geometry
        return result

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "AssemblyInstance":
        return AssemblyInstance(
            id=str(data["id"]),
            name=str(data.get("name", data["id"])),
            path=str(data.get("path", "")),
            position=list(data.get("position", [0.0, 0.0, 0.0])),
            rotation=data.get("rotation"),
            color=list(data.get("color", [0.36, 0.56, 0.76])),
            visible=bool(data.get("visible", True)),
            bbox=list(data["bbox"]) if data.get("bbox") is not None else None,
            local_origin=list(data.get("local_origin", [0.0, 0.0, 0.0])),
            mount_frame=data.get("mount_frame"),
            world_transform=data.get("world_transform"),
            mount_uv=list(data.get("mount_uv", [0.0, 0.0])),
            mount_normal_offset=float(data.get("mount_normal_offset", 0.0)),
        )

