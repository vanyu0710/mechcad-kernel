"""
测试 PersistentNamingResolver（v1.1 修复版）

P1-4 修复：必须显式 role
"""
import pytest
from mech_kernel.persistent_naming import (
    PersistentNamingResolver, PersistentName, AmbiguityError
)


def test_register_and_resolve():
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    
    # 解析必须显式 role
    resolved = r.resolve("main_body", "body")
    assert resolved == ("F_001", 0)


def test_resolve_without_role_raises():
    """不传 role 应该报错（API 强制显式）"""
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    with pytest.raises(TypeError):
        r.resolve("main_body")  # 缺 role


def test_resolve_safe_returns_none_for_unknown():
    r = PersistentNamingResolver()
    assert r.resolve_safe("unknown", "body") is None


def test_resolve_safe_returns_none_for_wrong_role():
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    # 用了不存在的 role
    assert r.resolve_safe("main_body", "top_face") is None


def test_register_overrides_same_key():
    """同 feature_id 同 role，重复注册会覆盖"""
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    r.register("F_001", "main_body_v2", role="body", geom_index=1)
    # 解析 "main_body" 应该找不到（v2 是不同的 name）
    assert r.resolve_safe("main_body", "body") is None
    # 但 "main_body_v2" 找得到
    assert r.resolve("main_body_v2", "body") == ("F_001", 1)


def test_multiple_features_same_name_same_role_raises_ambiguity():
    """同名同 role 的多个 feature 解析时抛 AmbiguityError"""
    r = PersistentNamingResolver()
    r.register("F_001", "hole", role="body", geom_index=0)
    r.register("F_002", "hole", role="body", geom_index=0)
    r.register("F_003", "hole", role="body", geom_index=0)
    
    with pytest.raises(AmbiguityError) as exc:
        r.resolve("hole", "body")
    assert "歧义" in str(exc.value)


def test_same_name_different_role_no_ambiguity():
    """同名不同 role 不算歧义"""
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    r.register("F_001", "main_body", role="top_face", geom_index=1)
    
    # 不同 role 各自唯一
    assert r.resolve("main_body", "body") == ("F_001", 0)
    assert r.resolve("main_body", "top_face") == ("F_001", 1)


def test_get_feature_id():
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    r.register("F_002", "hole_1", role="body", geom_index=0)
    
    assert r.get_feature_id("main_body", "body") == "F_001"
    assert r.get_feature_id("hole_1", "body") == "F_002"
    assert r.get_feature_id("nonexistent", "body") is None


def test_remove_feature():
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    r.register("F_001", "main_body", role="top_face", geom_index=1)
    r.remove_feature("F_001")
    
    # 删除后查询不到
    assert r.get_feature_id("main_body", "body") is None
    assert r.get_feature_id("main_body", "top_face") is None


def test_get_candidates():
    """可以查询所有候选"""
    r = PersistentNamingResolver()
    r.register("F_001", "hole", role="body", geom_index=0)
    r.register("F_002", "hole", role="body", geom_index=0)
    
    candidates = r.get_candidates("hole", "body")
    assert len(candidates) == 2


def test_snapshot_and_restore():
    r = PersistentNamingResolver()
    r.register("F_001", "main_body", role="body", geom_index=0)
    
    snap = r.snapshot()
    
    r.register("F_002", "hole_1", role="body", geom_index=0)
    assert r.get_feature_id("hole_1", "body") == "F_002"
    
    r.restore(snap)
    assert r.get_feature_id("hole_1", "body") is None
    assert r.get_feature_id("main_body", "body") == "F_001"


def test_persistent_name_equality_and_frozen():
    """PersistentName 用 frozen=True，自动生成 hash/eq"""
    n1 = PersistentName("main_body", "top_face")
    n2 = PersistentName("main_body", "top_face")
    n3 = PersistentName("main_body", "bottom_face")
    
    assert n1 == n2
    assert hash(n1) == hash(n2)
    assert n1 != n3
    
    # frozen=True: 不允许修改
    with pytest.raises(Exception):  # FrozenInstanceError
        n1.feature_name = "modified"


def test_persistent_name_validates_empty():
    """PersistentName 不接受空字符串"""
    with pytest.raises(ValueError):
        PersistentName("", "body")
    with pytest.raises(ValueError):
        PersistentName("main_body", "")


def test_ambiguity_error_lists_candidates():
    r = PersistentNamingResolver()
    r.register("F_001", "hole", role="body", geom_index=0)
    r.register("F_002", "hole", role="body", geom_index=0)
    
    with pytest.raises(AmbiguityError) as exc:
        r.resolve("hole", "body")
    assert exc.value.semantic_name == "hole"
    assert exc.value.role == "body"
    assert len(exc.value.candidates) == 2
