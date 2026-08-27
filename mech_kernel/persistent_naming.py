"""
MechKernel Persistent Naming（v1.1 修复版）

P1-4 修复：
- 改用 semantic_name -> role -> candidates[] 结构
- 查询必须显式 role
- 多候选返回歧义结果（不静默选最新）

专家审查原话：
"如果查询只按 semantic_name，body 和 top_face 会产生歧义，
最新时间戳可能导致非确定性覆盖。建议改成 semantic_name -> role -> candidates[]，
查询必须显式要求 role；多候选时返回 ambiguity，而不是静默选最新。"
"""
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
import time

from .features import Reference
from .errors import StateCorruptionError


@dataclass(frozen=True, eq=True)
class PersistentName:
    """
    持久语义名（P0-3 修复：frozen=True）。
    
    AI 调用时传这个：
        hole(workplane_name="base", position=(0, 0), diameter=10)
        fillet(edge_refs=[Reference.edge("top_edge", "main_body")])
    """
    feature_name: str              # "main_body" / "hole_1"
    role: str                      # "top_face" | "bottom_edge" | "axis" | "body" | ...
    
    def __post_init__(self):
        # 防御：feature_name 和 role 都不能为空
        if not self.feature_name:
            raise ValueError("PersistentName.feature_name 不能为空")
        if not self.role:
            raise ValueError("PersistentName.role 不能为空")


class NamingEntry:
    """一个引用条目"""
    def __init__(self, name: PersistentName, geom_signature: Any = None, geom_index: Optional[int] = None):
        self.name = name
        self.geom_signature = geom_signature
        self.geom_index = geom_index
        self.timestamp = time.time()
        self.feature_id = ""


class AmbiguityError(StateCorruptionError):
    """引用解析时发现多个候选，AI 需要进一步指定"""
    def __init__(self, semantic_name: str, role: str, candidates: List[Tuple[str, float]]):
        self.semantic_name = semantic_name
        self.role = role
        self.candidates = candidates
        super().__init__(
            f"引用歧义: ({semantic_name}, {role}) 有 {len(candidates)} 个候选: "
            + ", ".join(f"{fid}({ts:.0f})" for fid, ts in candidates[:3])
        )


class PersistentNamingResolver:
    """
    持久命名解析器（v1.1 修复版）。
    
    数据结构：semantic_name -> role -> List[NamingEntry]（按时间倒序）
    
    用法：
        resolver = PersistentNamingResolver()
        resolver.register("F_001", "main_body", role="body", geom_index=0)
        # 几何变化后
        resolver.update_after_topology_change(geometry)
        # 查询（必须显式 role）
        ref = resolver.resolve("main_body", "top_face", geometry)
    """
    
    def __init__(self):
        # semantic_name -> role -> List[(feature_id, timestamp, geom_index)]
        # 倒序：最新的在 [0]
        self._index: Dict[str, Dict[str, List[Tuple[str, float, Optional[int]]]]] = {}
        # 缓存：feature_id -> List[(semantic_name, role)]
        self._by_feature: Dict[str, List[Tuple[str, str]]] = {}
    
    def register(
        self, 
        feature_id: str, 
        semantic_name: str, 
        role: str = "body",
        geom_index: Optional[int] = None,
        geom_signature: Any = None
    ) -> PersistentName:
        """注册一个新的语义名
        
        P1-4 修复：
        - 同一个 feature_id + role 只能有一个 semantic_name
        - 重新注册时，从所有 semantic_name 中清除同 feature_id+role 的旧记录
        """
        if not semantic_name:
            raise ValueError("semantic_name 不能为空")
        if not role:
            raise ValueError("role 不能为空")
        if not feature_id:
            raise ValueError("feature_id 不能为空")
        
        name = PersistentName(semantic_name, role)
        now = time.time()
        
        # 0. 先从所有 semantic_name 中清除同 feature_id+role 的旧记录
        # （保证同 feature_id+role 只对应一个 semantic_name）
        for sn in list(self._index.keys()):
            if role in self._index[sn]:
                self._index[sn][role] = [
                    (fid, ts, gi) for (fid, ts, gi) in self._index[sn][role]
                    if fid != feature_id
                ]
                # 清理空列表
                if not self._index[sn][role]:
                    del self._index[sn][role]
                if not self._index[sn]:
                    del self._index[sn]
        
        # 清理反向索引中的旧记录
        if feature_id in self._by_feature:
            self._by_feature[feature_id] = [
                (sn, r) for (sn, r) in self._by_feature[feature_id]
                if not (sn == semantic_name and r == role)
            ]
        
        # 1. 主索引：semantic_name -> role -> [(feature_id, timestamp, geom_index)]
        if semantic_name not in self._index:
            self._index[semantic_name] = {}
        if role not in self._index[semantic_name]:
            self._index[semantic_name][role] = []
        
        # 插入新条目（最新在前）
        self._index[semantic_name][role].insert(0, (feature_id, now, geom_index))
        
        # 2. 反向索引
        if feature_id not in self._by_feature:
            self._by_feature[feature_id] = []
        if (semantic_name, role) not in self._by_feature[feature_id]:
            self._by_feature[feature_id].append((semantic_name, role))
        
        return name
    
    def resolve(
        self, 
        semantic_name: str, 
        role: str,
        geometry: Any = None
    ) -> Tuple[str, int]:
        """
        解析语义名到 (feature_id, geom_index)。
        
        P1-4 修复：必须显式 role，**不**做模糊匹配。
        
        Returns:
            (feature_id, geom_index)
        
        Raises:
            KeyError: 找不到任何候选
            AmbiguityError: 多个候选且无法决定
        """
        if semantic_name not in self._index:
            raise KeyError(f"未注册的语义名: {semantic_name}")
        if role not in self._index[semantic_name]:
            raise KeyError(f"未注册的 role: ({semantic_name}, {role})")
        
        candidates = self._index[semantic_name][role]
        if not candidates:
            raise KeyError(f"空候选列表: ({semantic_name}, {role})")
        
        if len(candidates) == 1:
            feature_id, _, geom_index = candidates[0]
            return (feature_id, geom_index if geom_index is not None else 0)
        
        # 多个候选：返回歧义
        raise AmbiguityError(
            semantic_name=semantic_name,
            role=role,
            candidates=[(fid, ts) for fid, ts, _ in candidates],
        )
    
    def resolve_safe(
        self, 
        semantic_name: str, 
        role: str,
        geometry: Any = None
    ) -> Optional[Tuple[str, int]]:
        """
        安全解析：失败返回 None 而非抛异常。
        """
        try:
            return self.resolve(semantic_name, role, geometry)
        except (KeyError, AmbiguityError):
            return None
    
    def _heuristic_match(
        self, 
        semantic_name: str, 
        role: str, 
        geometry: Any
    ) -> Optional[Tuple[str, int]]:
        """
        启发式匹配（几何变化后）。
        
        P1-4 修复：只在显式 role 下做启发式，不模糊匹配。
        """
        try:
            return self.resolve(semantic_name, role, geometry)
        except (KeyError, AmbiguityError):
            return None
    
    def update_after_topology_change(self, geometry: Any) -> None:
        """
        拓扑变化后，更新所有引用。
        
        M0 简化：只更新 timestamp，不做实际几何匹配。
        实际匹配需要 build123d 配合（M2+ 阶段）。
        """
        # M0 阶段：保持引用不变
        pass
    
    def remove_feature(self, feature_id: str) -> None:
        """删除一个 feature 的所有引用"""
        if feature_id not in self._by_feature:
            return
        for (semantic_name, role) in self._by_feature[feature_id]:
            if semantic_name in self._index and role in self._index[semantic_name]:
                self._index[semantic_name][role] = [
                    (fid, ts, gi) for (fid, ts, gi) in self._index[semantic_name][role]
                    if fid != feature_id
                ]
        del self._by_feature[feature_id]
    
    def all_names(self) -> List[PersistentName]:
        """列出所有已注册的语义名"""
        result = []
        for semantic_name, roles in self._index.items():
            for role, candidates in roles.items():
                if candidates:
                    feature_id, _, _ = candidates[0]
                    result.append(PersistentName(semantic_name, role))
        return result
    
    def get_candidates(self, semantic_name: str, role: str) -> List[Tuple[str, float]]:
        """获取候选列表（不抛异常，AI 可以看到所有候选）"""
        if semantic_name not in self._index or role not in self._index[semantic_name]:
            return []
        return [(fid, ts) for fid, ts, _ in self._index[semantic_name][role]]
    
    def get_feature_id(self, semantic_name: str, role: str = "body") -> Optional[str]:
        """根据语义名获取最新的 feature_id
        
        Returns:
            feature_id 字符串；不存在返回 None
        """
        result = self.resolve_safe(semantic_name, role)
        if result is None:
            return None
        return result[0]
    
    def snapshot(self) -> "PersistentNamingResolver":
        """创建快照"""
        import copy
        new = PersistentNamingResolver()
        new._index = copy.deepcopy(self._index)
        new._by_feature = copy.deepcopy(self._by_feature)
        return new
    
    def restore(self, other: "PersistentNamingResolver") -> None:
        """从快照恢复"""
        import copy
        self._index = copy.deepcopy(other._index)
        self._by_feature = copy.deepcopy(other._by_feature)
