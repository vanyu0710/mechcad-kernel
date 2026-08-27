"""
MechKernel Feature Graph（DAG）

替代 v1.0 的 List[FeatureNode]，支持：
- 拓扑排序
- 循环检测
- 依赖追踪
- 子图删除
"""
from typing import Dict, List, Set, Optional
from collections import deque

from .features import FeatureNode, FeatureState
from .errors import StateCorruptionError, KernelBugError


class FeatureGraph:
    """
    Feature 的 DAG 结构。
    
    节点: feature_id -> FeatureNode
    边: from_id -> [to_id]，表示 from 是 to 的依赖
    """
    
    def __init__(self):
        self.nodes: Dict[str, FeatureNode] = {}
        self.edges: Dict[str, List[str]] = {}      # forward: from_id -> [dependent_ids]
        self.reverse_edges: Dict[str, List[str]] = {}  # backward: to_id -> [dependency_ids]
    
    # === 基本操作 ===
    
    def add_node(self, node: FeatureNode) -> None:
        """添加节点"""
        if node.id in self.nodes:
            raise StateCorruptionError(f"Feature {node.id} 已存在")
        self.nodes[node.id] = node
        self.edges.setdefault(node.id, [])
        self.reverse_edges.setdefault(node.id, [])
    
    def add_edge(self, from_id: str, to_id: str) -> None:
        """添加边（from_id -> to_id，表示 from 是 to 的依赖）
        
        P1-7 修复：用增量 DFS 检测环，不再做全图扫描
        """
        if from_id not in self.nodes:
            raise StateCorruptionError(f"源 feature {from_id} 不存在")
        if to_id not in self.nodes:
            raise StateCorruptionError(f"目标 feature {to_id} 不存在")
        if from_id == to_id:
            raise StateCorruptionError(f"自环不允许: {from_id}")
        if to_id in self.edges[from_id]:
            return  # 重复边忽略
        
        # 增量环检测：加边 from_id -> to_id
        # 等价于检查 to_id 沿 forward 边能否到达 from_id
        # 如果能，from_id -> to_id -> ... -> from_id 形成环
        if self._reachable(to_id, from_id):
            raise StateCorruptionError(f"添加边 {from_id} -> {to_id} 会形成环")
        
        self.edges[from_id].append(to_id)
        self.reverse_edges[to_id].append(from_id)
    
    def _reachable(self, start_id: str, target_id: str) -> bool:
        """
        检查从 start_id 沿 forward 边能否到达 target_id。
        
        P1-7：增量 DFS/BFS，O(V+E) 但通常远小于全图。
        """
        if start_id == target_id:
            return True
        visited = set()
        stack = [start_id]
        while stack:
            nid = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)
            for next_id in self.edges.get(nid, []):
                if next_id == target_id:
                    return True
                if next_id not in visited:
                    stack.append(next_id)
        return False
    
    def get_node(self, feature_id: str) -> FeatureNode:
        """获取节点"""
        if feature_id not in self.nodes:
            raise StateCorruptionError(f"Feature {feature_id} 不存在")
        return self.nodes[feature_id]
    
    def has_node(self, feature_id: str) -> bool:
        return feature_id in self.nodes
    
    def get_dependencies(self, feature_id: str) -> List[FeatureNode]:
        """获取所有依赖（被依赖的 feature）"""
        dep_ids = self.reverse_edges.get(feature_id, [])
        return [self.nodes[did] for did in dep_ids if did in self.nodes]
    
    def add(self, node: FeatureNode) -> None:
        """快捷：加节点（add_node 别名）"""
        self.add_node(node)
    
    def to_dict(self) -> dict:
        return {
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "edges": {k: list(v) for k, v in self.edges.items()},
            "reverse_edges": {k: list(v) for k, v in self.reverse_edges.items()},
        }
    
    def from_dict(self, data: dict) -> None:
        from .features import FeatureNode
        self.nodes = {k: FeatureNode.from_dict(v) for k, v in data.get("nodes", {}).items()}
        self.edges = {k: list(v) for k, v in data.get("edges", {}).items()}
        self.reverse_edges = {k: list(v) for k, v in data.get("reverse_edges", {}).items()}
    
    def get_dependents(self, feature_id: str) -> List[FeatureNode]:
        """获取所有依赖此 feature 的下游"""
        dep_ids = self.edges.get(feature_id, [])
        return [self.nodes[did] for did in dep_ids if did in self.nodes]
    
    def all_nodes(self) -> List[FeatureNode]:
        """所有节点"""
        return list(self.nodes.values())
    
    def __len__(self) -> int:
        return len(self.nodes)
    
    # === 图算法 ===
    
    def topological_sort(self) -> List[str]:
        """拓扑排序，依赖在前，被依赖在后。"""
        in_degree: Dict[str, int] = {nid: 0 for nid in self.nodes}
        for nid in self.nodes:
            for dep_id in self.reverse_edges.get(nid, []):
                in_degree[nid] += 1
        
        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        result: List[str] = []
        
        while queue:
            nid = queue.popleft()
            result.append(nid)
            for dep_id in self.edges.get(nid, []):
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)
        
        if len(result) != len(self.nodes):
            raise StateCorruptionError("Feature Graph 存在环，无法拓扑排序")
        return result
    
    def has_cycle(self) -> bool:
        """检测是否有环（BFS）"""
        if not self.nodes:
            return False
        in_degree: Dict[str, int] = {nid: 0 for nid in self.nodes}
        for nid in self.nodes:
            for dep_id in self.reverse_edges.get(nid, []):
                in_degree[nid] += 1
        
        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        visited_count = 0
        while queue:
            nid = queue.popleft()
            visited_count += 1
            for dep_id in self.edges.get(nid, []):
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)
        return visited_count != len(self.nodes)
    
    # === 子图操作 ===
    
    def remove_node(self, feature_id: str) -> None:
        """删除节点（同时清理所有相关边）"""
        if feature_id not in self.nodes:
            return  # 幂等
        
        # 清理上游边的反向引用
        for upstream_id in list(self.reverse_edges.get(feature_id, [])):
            if feature_id in self.edges.get(upstream_id, []):
                self.edges[upstream_id].remove(feature_id)
        # 清理下游边的反向引用
        for downstream_id in list(self.edges.get(feature_id, [])):
            if feature_id in self.reverse_edges.get(downstream_id, []):
                self.reverse_edges[downstream_id].remove(feature_id)
        
        del self.nodes[feature_id]
        self.edges.pop(feature_id, None)
        self.reverse_edges.pop(feature_id, None)
    
    def get_descendants(self, feature_id: str) -> List[str]:
        """获取所有下游节点（递归，BFS）"""
        result: Set[str] = set()
        queue = deque(self.edges.get(feature_id, []))
        while queue:
            nid = queue.popleft()
            if nid in result:
                continue
            result.add(nid)
            queue.extend(self.edges.get(nid, []))
        return list(result)
    
    def get_ancestors(self, feature_id: str) -> List[str]:
        """获取所有上游节点（递归，BFS）"""
        result: Set[str] = set()
        queue = deque(self.reverse_edges.get(feature_id, []))
        while queue:
            nid = queue.popleft()
            if nid in result:
                continue
            result.add(nid)
            queue.extend(self.reverse_edges.get(nid, []))
        return list(result)
    
    # === 快照 / 恢复 ===
    
    def snapshot(self) -> "FeatureGraph":
        """创建完整快照（深拷贝）"""
        import copy
        new_graph = FeatureGraph()
        for nid, node in self.nodes.items():
            new_node = copy.deepcopy(node)
            new_graph.nodes[nid] = new_node
        for fid, deps in self.edges.items():
            new_graph.edges[fid] = list(deps)
        for fid, revs in self.reverse_edges.items():
            new_graph.reverse_edges[fid] = list(revs)
        return new_graph
    
    def restore(self, other: "FeatureGraph") -> None:
        """从快照恢复"""
        import copy
        self.nodes = copy.deepcopy(other.nodes)
        self.edges = {k: list(v) for k, v in other.edges.items()}
        self.reverse_edges = {k: list(v) for k, v in other.reverse_edges.items()}
