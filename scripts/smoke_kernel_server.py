#!/usr/bin/env python
"""MechKernel worker server 真子进程冒烟（P0 验收）。

以子进程方式启动 ``mech_kernel/server.py``（stdio JSON-lines RPC），
按路线图 P0 验收驱动：capabilities → box+hole 建模 → feature_tree →
select_refs → validate → 导出 STEP/STL → undo/redo → snapshot/restore → shutdown。

任一步失败即非零退出。用法::

    # 仓库根目录（或任意 cwd，脚本自动定位仓库根）
    python scripts/smoke_kernel_server.py
    # 指定解释器（默认 sys.executable，CI 可传 kernel venv python）
    python scripts/smoke_kernel_server.py --python path/to/python.exe
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "mech_kernel" / "server.py"

BOX = 120.0
THICK = 12.0
HOLE_D = 30.0

BOX_VOLUME = BOX * BOX * THICK
HOLE_VOLUME = math.pi * (HOLE_D / 2) ** 2 * THICK
FINAL_VOLUME = BOX_VOLUME - HOLE_VOLUME

_request_id = 0


class SmokeFailure(AssertionError):
    pass


class ServerHandle:
    def __init__(self, python: str) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [python, str(SERVER)],
            cwd=str(REPO_ROOT),  # kernel 未 pip 安装，靠 cwd 导入 mech_kernel
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            encoding="utf-8",
        )

    def request(self, cmd: str, payload: dict | None = None, timeout: float = 120) -> dict:
        global _request_id
        _request_id += 1
        line = json.dumps({"id": str(_request_id), "cmd": cmd, "payload": payload or {}}, ensure_ascii=False)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        try:
            response_line = self.proc.stdout.readline()
        except Exception as exc:  # noqa: BLE001
            raise SmokeFailure(f"读取响应失败（cmd={cmd}）: {exc}") from exc
        if not response_line:
            stderr_tail = self.proc.stderr.read()[-2000:] if self.proc.stderr else ""
            raise SmokeFailure(f"server 无响应（cmd={cmd}，可能崩溃）\nstderr 尾部:\n{stderr_tail}")
        return json.loads(response_line)

    def ok(self, cmd: str, payload: dict | None = None) -> dict:
        resp = self.request(cmd, payload)
        if not resp.get("ok"):
            raise SmokeFailure(f"{cmd} RPC 失败: {resp.get('error')}")
        return resp["data"]

    def execute(self, op: str, **args) -> dict:
        data = self.ok("execute", {"op": op, "args": args})
        if not data.get("success"):
            raise SmokeFailure(f"execute({op}) 失败: {data.get('error_kind')}: {data.get('error')}")
        return data

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise SmokeFailure(f"✗ {name} {detail}")
    print(f"  ok {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable, help="server 子进程解释器")
    args = parser.parse_args()

    print(f"冒烟: {SERVER}")
    print(f"解释器: {args.python}")
    server = ServerHandle(args.python)
    try:
        # 1. 健康检查 + 能力清单
        ping = server.ok("ping")
        check("ping", ping.get("pong") is True, f"version={ping.get('kernel_version')}")

        caps = server.ok("capabilities")
        check("capabilities 33 ops", caps.get("public_count") == 33, f"got {caps.get('public_count')}")
        check("experimental 10 ops", len(caps.get("experimental", [])) == 10)
        tool_names = {c["name"] for c in caps["public"]}
        check("schema 可用于 LLM tools", {"extrude", "hole", "select", "set_parameter"} <= tool_names)

        # 2. box + hole 建模（路线图 P0 验收序列）
        server.execute("create_workplane", name="base", type="XY")
        server.execute("new_sketch", workplane_name="base", sketch_name="sk_box")
        server.execute("add_rectangle", sketch_name="sk_box", width=BOX, height=BOX, name="plate")
        server.execute("close_sketch", sketch_name="sk_box")
        extrude = server.execute("extrude", sketch_name="sk_box", depth=THICK, mode="new_body", name="plate_body")
        check("box 体积", abs(extrude["geometry_summary"]["volume"] - BOX_VOLUME) < 1.0,
              f"vol={extrude['geometry_summary']['volume']:.1f}")

        before_hole = server.ok("query", {"target": "_current_geometry", "what": "volume"})["value"]
        server.execute("hole", position=[0, 0], diameter=HOLE_D, name="center_bore")
        after_hole = server.ok("query", {"target": "_current_geometry", "what": "volume"})["value"]
        check("hole 体积减少", abs((before_hole - after_hole) - HOLE_VOLUME) < 5.0,
              f"delta={before_hole - after_hole:.1f} expect={HOLE_VOLUME:.1f}")

        # 3. feature_tree + select_refs + validate
        tree = server.ok("feature_tree")
        check("feature_tree 有图", tree["node_count"] >= 2 and len(tree["op_history"]) == 6,
              f"nodes={tree['node_count']} history={len(tree['op_history'])}")
        faces = server.ok("select_refs", {"filter_type": "plane", "element_type": "face"})
        refs = [s["ref"] for s in faces["value"]["selected"]]
        check("select_refs 平面引用", len(refs) >= 2, f"refs={refs}")
        val = server.execute("validate_geometry", level="standard")
        check("validate_geometry", val["success"] is True)

        # 4. 导出 STEP + STL
        with tempfile.TemporaryDirectory() as td:
            step_path = os.path.join(td, "smoke.step")
            server.execute("export", path=step_path, format="step")
            check("export STEP", os.path.getsize(step_path) > 0)
            stl_path = os.path.join(td, "smoke.stl")
            mesh = server.ok("export_mesh", {"path": stl_path, "format": "stl"})
            check("export_mesh STL", mesh["size"] > 0 and os.path.getsize(stl_path) > 0)

        # 5. undo / redo / snapshot / restore（export 也进历史，计数在导出后取）
        nodes_before = server.ok("feature_tree")["node_count"]
        server.ok("undo", {"steps": 1})  # 撤销最后一步（export 节点）
        tree2 = server.ok("feature_tree")
        check("undo 生效", tree2["node_count"] == nodes_before - 1)
        server.ok("redo", {"steps": 1})
        check("redo 生效", server.ok("feature_tree")["node_count"] == nodes_before)

        server.ok("snapshot")
        history_len = len(server.ok("feature_tree")["op_history"])
        server.ok("undo", {"steps": history_len})
        check("全撤销后图空", server.ok("feature_tree")["node_count"] == 0)
        server.ok("restore")
        vol = server.ok("query", {"target": "_current_geometry", "what": "volume"})["value"]
        check("restore 恢复体积", abs(vol - FINAL_VOLUME) < 5.0, f"vol={vol:.1f}")

        # 6. 干净退出
        bye = server.ok("shutdown")
        check("shutdown", bye.get("bye") is True)
    finally:
        server.close()

    print("\n冒烟全部通过 ✔")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as exc:
        print(f"\n冒烟失败: {exc}", file=sys.stderr)
        sys.exit(1)
