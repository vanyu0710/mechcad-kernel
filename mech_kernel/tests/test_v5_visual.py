"""v2.2 visual contract tests."""
import base64
import io
import math

from mech_kernel import MechKernel
from mech_kernel.adaptive_renderer import AdaptiveRenderer
from mech_kernel.kernel import PUBLIC_OPS
from mech_kernel.renderer import Renderer


def _png(color):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(out, format="PNG")
    return out.getvalue()


def test_render_is_public_and_has_schema():
    k = MechKernel()
    assert "render" in PUBLIC_OPS
    assert k.cap.has("render")
    assert set(k.cap.get("render").input_schema) == {
        "views", "size", "annotate", "section", "turntable", "intent", "target", "name"
    }


def test_render_without_geometry_is_recoverable():
    r = MechKernel().execute("render")
    assert not r.success
    assert r.error_kind == "RECOVERABLE"


def test_compose_grid_single_and_multiple_images():
    one = Renderer.compose_grid({"iso": _png("red")})
    many = Renderer.compose_grid({"iso": _png("red"), "front": _png("blue")})
    assert one and many
    assert len(many) > len(one)


def test_adaptive_visual_budget():
    ar = AdaptiveRenderer(interval=3)
    assert ar.should_render("extrude", has_geometry=True) == "full"
    assert ar.should_render("add_circle", has_geometry=True) == "none"
    assert ar.should_render("add_circle", has_geometry=True) == "none"
    assert ar.should_render("add_circle", has_geometry=True) == "iso_only"
    assert ar.should_render("query", has_geometry=True) == "none"
    assert AdaptiveRenderer().should_render("assemble", has_geometry=True) == "full"


def test_render_views_and_section_contract():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 10)
    k.close_sketch("s")
    r = k.extrude("s", 10)
    assert r.render_level == "full"
    assert set(r.render_views) == {"iso", "front", "top", "side"}
    rr = k.execute("render", views=["iso", "front"], size=160)
    assert rr.success and set(rr.render_views) == {"iso", "front"}
    assert rr.render_base64
    assert base64.b64decode(k.get_last_render_base64())
    assert rr.evidence_manifest["intent"] == "inspect"
    assert rr.evidence_manifest["projection"] == "orthographic"
    assert set(rr.evidence_manifest["image_hashes"]) == {"iso", "front"}
    section = k.render(section={"axis": "Z"}, size=160)
    assert section.success
    assert set(section.render_views) == {"iso", "front", "top", "side"}
    turntable = k.render(turntable=True, size=160)
    assert turntable.success and len(turntable.render_views) == 8
    assert turntable.evidence_manifest["layout"]["columns"] == 4
    from PIL import Image
    packet = Image.open(io.BytesIO(base64.b64decode(turntable.render_base64)))
    assert packet.width <= 160 and packet.height <= 160
    focus = k.render(intent="feature_zoom", target=r.feature_id, size=160)
    assert focus.success and focus.evidence_manifest["target"] == r.feature_id
    delta = k.render(intent="delta", target=r.feature_id, size=160)
    assert delta.success and set(delta.render_views) == {
        "before_iso", "before_front", "after_iso", "after_front"
    }
    original = k.query("_current_geometry", "volume").value
    section_volume = k._section_half(k._current_geometry, "Z").volume
    assert math.isclose(section_volume, original / 2.0, rel_tol=1e-6)
    assert k.query("_current_geometry", "volume").value == original
