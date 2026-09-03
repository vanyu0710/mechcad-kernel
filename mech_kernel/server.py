"""MechKernel worker server — 常驻子进程的 stdio JSON-lines RPC 薄壳。

协议（UTF-8，每行一个 JSON 对象）：

请求::

    {"id": "<str>", "cmd": "<command>", "payload": {...}}

响应::

    {"id": "<str>", "ok": true, "data": {...}}
    {"id": "<str>", "ok": false, "error": {"kind": "...", "message": "..."}}

要点：
- server 持有一个 ``MechKernel`` 实例（一会话一实例由调用方保证）。
- 每个请求独立 try/except：单个命令失败不带走进程。
- ``execute`` 返回的失败 ``StepResult`` 是成功的 RPC（``ok: true``，
  ``data.success == false``）；RPC 级 ``ok: false`` 只表示协议/内部错误。
- 渲染 PNG 以 base64 传输（``render_base64`` / ``render_views_base64``）。
- 日志与 traceback 走 stderr，stdout 只承载协议行。

命令一览::

    ping             健康检查 → {"pong": true, "kernel_version": ...}
    capabilities     公开/实验 op 的 LLM schema（cap.list_public/list_experimental）
    execute          {op, args, allow_experimental} → StepResult JSON
    feature_tree     feature_graph.to_dict()
    select_refs      select(filter_type, element_type, face_index) → StepResult
    update_feature   {feature_id, new_params} → StepResult
    delete_feature   {feature_id} → StepResult
    undo / redo      {steps} → StepResult
    rebuild          {name} → StepResult
    query            {target, what} → StepResult
    measure          {target1, target2, metric} → StepResult
    validate_geometry {target, level} → StepResult
    render           render(...) → StepResult（含 base64 渲染图）
    export           {path, format="step"} → StepResult（内核参数化历史内）
    export_mesh      {path, format="stl", tolerance} → 直接网格导出（不进历史）
    save_project     {base_path} → 落盘 STEP + graph + history
    load_project     {base_path, mode, name} → StepResult
    snapshot         内存快照（kernel._snapshot，server 内保存）
    restore          恢复 snapshot 保存的状态
    state            kernel.get_state()
    shutdown         应答后退出进程
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, TextIO, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mech_kernel import MechKernel, StepResult  # noqa: E402  (imports trigger _runtime_compat)

KERNEL_VERSION = getattr(__import__("mech_kernel"), "__version__", "unknown")


def _jsonable(value: Any) -> Any:
    """把任意内核返回值转成 JSON 安全结构（bytes → base64，tuple/set → list）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    return str(value)


def _step_to_dict(result: StepResult, *, include_render: bool = False) -> Dict[str, Any]:
    """StepResult → JSON dict。``value`` 是各 op 动态挂的结果字段（select/query/rebuild 等）。"""
    data = result.to_summary_dict()
    data["current_narrative"] = list(result.current_narrative)
    data["semantic_state"] = _jsonable(result.semantic_state)
    data["feature_graph_delta"] = _jsonable(result.feature_graph_delta)
    data["hint"] = result.hint
    dynamic_value = getattr(result, "value", None)
    if dynamic_value is not None:
        data["value"] = _jsonable(dynamic_value)
    if include_render:
        if result.render_base64:
            data["render_base64"] = result.render_base64
        if result.render_views:
            data["render_views_base64"] = {
                name: base64.b64encode(png).decode("ascii")
                for name, png in result.render_views.items()
            }
    return data


class KernelServer:
    """单实例内核 + 命令分派。``handle_line`` 可脱离 stdio 直接单测。"""

    def __init__(self) -> None:
        self.kernel = MechKernel()
        self._memory_snapshot: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _payload(payload: Any) -> Dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")
        return payload

    @staticmethod
    def _require(payload: Dict[str, Any], key: str) -> Any:
        if key not in payload or payload[key] is None:
            raise ValueError(f"payload 缺少必填字段: {key}")
        return payload[key]

    def _current_geometry_or_fail(self) -> Any:
        geometry = self.kernel._current_geometry
        if geometry is None:
            raise ValueError("当前没有几何（先建模再导出）")
        return geometry

    # --------------------------------------------------------------- dispatch
    def dispatch(self, cmd: str, payload: Any) -> Any:
        """执行一条命令，返回可 JSON 序列化的 data。抛异常 = RPC 级错误。"""
        payload = self._payload(payload)

        if cmd == "ping":
            return {"pong": True, "kernel_version": KERNEL_VERSION}

        if cmd == "capabilities":
            return {
                "public": self.kernel.cap.list_public(),
                "experimental": self.kernel.cap.list_experimental(),
                "public_count": len(self.kernel.cap.list_public()),
            }

        if cmd == "execute":
            op = self._require(payload, "op")
            if not isinstance(op, str):
                raise ValueError("op 必须是字符串")
            args = payload.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args 必须是 JSON 对象")
            allow_experimental = bool(payload.get("allow_experimental", False))
            result = self.kernel.execute(op, allow_experimental=allow_experimental, **args)
            return _step_to_dict(result, include_render=bool(payload.get("include_render", False)))

        if cmd == "feature_tree":
            graph = self.kernel.feature_graph.to_dict()
            return {
                "graph": _jsonable(graph),
                "node_count": len(graph.get("nodes", {})),
                "op_history": _jsonable(self.kernel._op_history),
                "narrative": list(self.kernel.narrative),
                "parameters": _jsonable(getattr(self.kernel, "_parameters", {})),
            }

        if cmd == "select_refs":
            result = self.kernel.select(
                filter_type=payload.get("filter_type", "all"),
                face_index=payload.get("face_index"),
                element_type=payload.get("element_type", "face"),
            )
            return _step_to_dict(result)

        if cmd == "update_feature":
            result = self.kernel.update_feature(
                self._require(payload, "feature_id"),
                self._require(payload, "new_params"),
            )
            return _step_to_dict(result)

        if cmd == "delete_feature":
            result = self.kernel.delete_feature(self._require(payload, "feature_id"))
            return _step_to_dict(result)

        if cmd in ("undo", "redo"):
            method = getattr(self.kernel, cmd)
            result = method(steps=int(payload.get("steps", 1)))
            return _step_to_dict(result)

        if cmd == "rebuild":
            result = self.kernel.rebuild(name=payload.get("name", ""))
            return _step_to_dict(result)

        if cmd == "query":
            result = self.kernel.query(
                self._require(payload, "target"),
                what=payload.get("what", "bounding_box"),
            )
            return _step_to_dict(result)

        if cmd == "measure":
            result = self.kernel.measure(
                self._require(payload, "target1"),
                target2=payload.get("target2"),
                metric=payload.get("metric", "distance"),
            )
            return _step_to_dict(result)

        if cmd == "validate_geometry":
            result = self.kernel.validate_geometry(
                target=payload.get("target", "_current_geometry"),
                level=payload.get("level", "standard"),
            )
            return _step_to_dict(result)

        if cmd == "render":
            allowed = {
                "views", "size", "annotate", "section", "turntable", "intent",
                "target", "name", "quality", "backend", "show_edges", "highlight",
            }
            kwargs = {k: v for k, v in payload.items() if k in allowed}
            result = self.kernel.render(**kwargs)
            return _step_to_dict(result, include_render=True)

        if cmd == "export":
            result = self.kernel.export(
                self._require(payload, "path"),
                format=payload.get("format", "step"),
            )
            return _step_to_dict(result)

        if cmd == "export_mesh":
            return self._export_mesh(payload)

        if cmd == "save_project":
            return _jsonable(self.kernel.save_project(self._require(payload, "base_path")))

        if cmd == "load_project":
            result = self.kernel.load_project(
                self._require(payload, "base_path"),
                mode=payload.get("mode", "new_body"),
                name=payload.get("name", "loaded_project"),
            )
            return _step_to_dict(result)

        if cmd == "snapshot":
            self._memory_snapshot = self.kernel._snapshot()
            return {"saved": True, "op_history_len": len(self.kernel._op_history)}

        if cmd == "restore":
            if self._memory_snapshot is None:
                raise ValueError("没有可恢复的快照（先调用 snapshot）")
            self.kernel._restore(self._memory_snapshot)
            return {"restored": True, "op_history_len": len(self.kernel._op_history)}

        if cmd == "state":
            return _jsonable(self.kernel.get_state())

        if cmd == "shutdown":
            return {"bye": True}

        raise KeyError(f"未知命令: {cmd}")

    def _export_mesh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """STL 网格导出：作用于当前几何，不进参数化历史（kernel.export 只支持 STEP）。"""
        from mech_kernel._runtime_compat import ensure_build123d_import

        ensure_build123d_import()
        from build123d import export_stl

        fmt = payload.get("format", "stl")
        if fmt != "stl":
            raise ValueError(f"export_mesh 当前只支持 format='stl'（收到 {fmt}）")
        geometry = self._current_geometry_or_fail()
        path = str(self._require(payload, "path"))
        tolerance = float(payload.get("tolerance", 0.001))
        angular_tolerance = float(payload.get("angular_tolerance", 0.1))
        exported = export_stl(
            geometry,
            path,
            tolerance=tolerance,
            angular_tolerance=angular_tolerance,
        )
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if not exported or size == 0:
            raise ValueError(f"STL 导出失败: {path}")
        return {"path": path, "format": fmt, "size": size}

    # ------------------------------------------------------------------- io
    def handle_line(self, line: str) -> Optional[Dict[str, Any]]:
        """处理一行请求，返回响应 dict（None 表示该行应被忽略，如空行/注释）。"""
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return None
        request_id: Any = None
        try:
            request = json.loads(stripped)
            if not isinstance(request, dict):
                raise ValueError("请求必须是 JSON 对象")
            request_id = request.get("id")
            cmd = request.get("cmd")
            if not isinstance(cmd, str) or not cmd:
                raise ValueError("请求缺少 cmd 字段")
            data = self.dispatch(cmd, request.get("payload"))
            return {"id": request_id, "ok": True, "data": data}
        except KeyError as exc:
            return {
                "id": request_id,
                "ok": False,
                "error": {"kind": "UNKNOWN_CMD", "message": str(exc.args[0])},
            }
        except ValueError as exc:
            return {
                "id": request_id,
                "ok": False,
                "error": {"kind": "BAD_REQUEST", "message": str(exc)},
            }
        except Exception as exc:  # noqa: BLE001 —— 单请求失败不带走进程
            print(f"[server] 请求异常 id={request_id}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return {
                "id": request_id,
                "ok": False,
                "error": {"kind": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"},
            }

    def serve(self, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
        """主循环。返回退出码。"""
        while True:
            line = stdin.readline()
            if not line:  # EOF —— 父进程退出/管道关闭
                print("[server] stdin EOF，退出", file=stderr)
                return 0
            response = self.handle_line(line)
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
            data = response.get("data")
            if response.get("ok") and isinstance(data, dict) and data.get("bye"):
                print("[server] shutdown 命令，退出", file=stderr)
                return 0


def main() -> int:
    # Windows 管道默认可能是 cp936/gbk，协议强制 UTF-8
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    server = KernelServer()
    print(
        f"[server] MechKernel {KERNEL_VERSION} ready (pid={os.getpid()})",
        file=sys.stderr,
    )
    return server.serve(sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
