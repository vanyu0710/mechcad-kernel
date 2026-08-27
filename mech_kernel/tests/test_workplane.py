"""
测试 Workplane
"""
import pytest
from mech_kernel.workplane import Workplane, WorkplaneType, WorkplaneRegistry
from mech_kernel.errors import InvalidRequestError


def test_create_xy_workplane():
    wp = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    assert wp.normal == (0.0, 0.0, 1.0)
    assert wp.x_dir == (1.0, 0.0, 0.0)


def test_create_yz_workplane():
    wp = Workplane(id="WP_001", name="side", type=WorkplaneType.YZ)
    assert wp.normal == (1.0, 0.0, 0.0)


def test_face_workplane_requires_reference():
    with pytest.raises(InvalidRequestError):
        Workplane(id="WP_001", name="face_wp", type=WorkplaneType.FACE)


def test_registry_register_and_get():
    reg = WorkplaneRegistry()
    wp = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    reg.register(wp)
    assert reg.has_name("base")
    assert reg.has_id("WP_001")
    assert reg.get_by_name("base").id == "WP_001"


def test_registry_duplicate_id_raises():
    reg = WorkplaneRegistry()
    wp1 = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    wp2 = Workplane(id="WP_001", name="other", type=WorkplaneType.XY)
    reg.register(wp1)
    with pytest.raises(InvalidRequestError):
        reg.register(wp2)


def test_registry_duplicate_name_raises():
    reg = WorkplaneRegistry()
    wp1 = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    wp2 = Workplane(id="WP_002", name="base", type=WorkplaneType.YZ)
    reg.register(wp1)
    with pytest.raises(InvalidRequestError):
        reg.register(wp2)


def test_registry_remove():
    reg = WorkplaneRegistry()
    wp = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    reg.register(wp)
    reg.remove("WP_001")
    assert not reg.has_id("WP_001")
    assert not reg.has_name("base")


def test_registry_snapshot_restore():
    reg = WorkplaneRegistry()
    wp1 = Workplane(id="WP_001", name="base", type=WorkplaneType.XY)
    reg.register(wp1)
    snap = reg.snapshot()
    
    wp2 = Workplane(id="WP_002", name="top", type=WorkplaneType.XY)
    reg.register(wp2)
    assert len(reg.all()) == 2
    
    reg.restore(snap)
    assert len(reg.all()) == 1
    assert not reg.has_name("top")
