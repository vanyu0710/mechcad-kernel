"""v2.5 renderer backend and instance-level assembly contracts."""
import base64
import io
import tempfile
from pathlib import Path

from PIL import Image

from mech_kernel import MechKernel
from mech_kernel.renderer import Renderer


def _part_step(path, radius=10, depth=20):
    from build123d import Cylinder
    from build123d.exporters3d import export_step
    export_step(Cylinder(radius, depth), str(path))


def test_renderer_backend_auto_records_headless_fallback():
    from build123d import Box

    renderer = Renderer(image_size=(160, 160), backend="auto")
    views = renderer.render(Box(10, 10, 10), level="full", views=["iso"], image_size=(120, 120))
    assert views["iso"]
    assert renderer.last_backend_used in {"occ", "matplotlib"}
    if renderer.last_backend_used == "matplotlib":
        assert renderer.last_warnings


def test_renderer_constructor_backend_and_presentation():
    from build123d import Box

    renderer = Renderer(image_size=(160, 160), backend="matplotlib")
    views = renderer.render(Box(10, 10, 10), level="full", views=["iso"], quality="presentation")
    assert views["iso"]
    assert renderer.last_backend_used == "matplotlib"


def test_hidden_assembly_instance_does_not_fall_back_to_fused_geometry():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        case = tmp_path / "case.step"
        cap = tmp_path / "cap.step"
        _part_step(case)
        _part_step(cap, radius=6, depth=8)

        kernel = MechKernel()
        assembled = kernel.assemble([
            {"path": str(case), "name": "case"},
            {"path": str(cap), "name": "cap", "position": [0, 0, 20]},
        ])
        assert assembled.success
        volume = kernel._current_geometry.volume
        instances = kernel.query_assembly().value["instances"]
        assert [item["id"] for item in instances] == ["A_0001", "A_0002"]
        kernel.set_instance_visibility("A_0001", False)
        kernel.set_instance_visibility("A_0002", False)
        assert kernel._current_geometry.volume == volume
        hidden = kernel.render(size=160, backend="matplotlib")
        assert not hidden.success
        assert hidden.error_kind == "RECOVERABLE"


def test_assembly_color_and_manifest_are_render_only():
    with tempfile.TemporaryDirectory() as tmp:
        step = Path(tmp) / "part.step"
        _part_step(step)
        kernel = MechKernel()
        kernel.assemble([{"path": str(step), "name": "nozzle", "color": [0.9, 0.2, 0.1]}])
        before = kernel._current_geometry.volume
        changed = kernel.set_instance_color("A_0001", [0.1, 0.2, 0.9])
        assert changed.success
        rendered = kernel.render(size=160, backend="matplotlib", highlight=["A_0001"])
        assert rendered.success
        assert rendered.scene_manifest["instance_ids"] == ["A_0001"]
        assert rendered.evidence_manifest["instance_ids"] == ["A_0001"]
        assert rendered.evidence_manifest["highlighted"] == ["A_0001"]
        assert kernel._current_geometry.volume == before
        packet = Image.open(io.BytesIO(base64.b64decode(rendered.render_base64)))
        assert packet.width <= 160 and packet.height <= 160
