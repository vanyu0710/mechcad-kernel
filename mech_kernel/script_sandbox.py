"""MechKernel v2.13: run_script 沙箱 —— 模型编写建模脚本，只准调用 kernel 公开 op。

DSH（DeepSeek Harness）式边界，几何主权永久归内核：
- **AST 静态校验**：import 只许 `math`（build123d 不在白名单——不允许裸几何）；
  禁危险模块/危险调用名；**一切 `_` 开头的属性访问拒绝**（杜绝 `k._current_geometry=...`
  之类绕过门面直改内核内部）；代码长度限制。
- **运行时命名空间**：`k`（ScriptKernel 门面）+ `math` + 安全 builtins
  （无 open/exec/eval/__import__/input/globals/locals/vars/dir）。
- **门面白名单**：只绑定 PUBLIC_OPS（减去 SCRIPT_OP_BLACKLIST）为同名方法 +
  带守卫的 `execute()`；导出/存档类 op 在脚本内禁用（归档由上层 finish_part 负责）。

脚本里执行的每个 op 照常 `_record_history` → 进 `_op_history`/`feature_graph` →
**代码件与 op 件一样可参数重放、可特征树编辑**。
"""
from __future__ import annotations

import ast
import builtins as _builtins

MAX_CODE_CHARS = 20000
ALLOWED_IMPORTS = {"math"}
FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "socket", "pathlib", "shutil", "requests",
    "urllib", "importlib", "tempfile", "ctypes", "threading", "multiprocessing",
    "signal", "platform", "site", "builtins",
}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "input",
    "globals", "locals", "vars", "dir", "breakpoint", "exit", "quit",
    "memoryview", "getattr", "setattr", "delattr", "hasattr", "super",
    "classmethod", "staticmethod", "property", "object", "type",
}
# 脚本内禁用的状态/IO op：导出与存档由上层（aicad finish_part / worker RPC）统一负责
SCRIPT_OP_BLACKLIST = {"export", "save_project", "load_project", "run_script"}


class ScriptSandboxError(ValueError):
    """脚本静态校验失败。message 面向 LLM，要能指导改脚本。"""


class ScriptOpError(RuntimeError):
    """脚本内某个建模 op 返回 success=false（v2.16 abort 策略）。

    携带结构化失败信息（op 名、实参、内层 error_kind/error/suggestion），
    让 run_script 能整体回滚并回传 failed_op，而不是让半成品静默交付。
    脚本内可用 `try/except Exception` 做条件回退（本异常是 RuntimeError 子类）。
    """

    def __init__(self, op: str, op_args: tuple, op_kwargs: dict, result) -> None:
        self.op = op
        self.op_args = op_args
        self.op_kwargs = op_kwargs
        self.inner_error_kind = getattr(result, "error_kind", None) or (
            result.get("error_kind") if isinstance(result, dict) else None)
        self.inner_error = getattr(result, "error", None) or (
            result.get("error") if isinstance(result, dict) else None)
        self.inner_suggestion = getattr(result, "suggestion", None) or (
            result.get("suggestion") if isinstance(result, dict) else None)
        super().__init__(f"op {op} 失败: {self.inner_error_kind}: {self.inner_error}")


def validate_script_code(code: str) -> ast.Module:
    """AST 白名单校验；违规抛 ScriptSandboxError（含可读原因）。"""
    if not isinstance(code, str) or not code.strip():
        raise ScriptSandboxError("code 不能为空")
    if len(code) > MAX_CODE_CHARS:
        raise ScriptSandboxError(f"code 超长（{len(code)} > {MAX_CODE_CHARS} 字符），请拆成多个脚本")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ScriptSandboxError(f"语法错误（第 {exc.lineno} 行）: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            else:
                modules = [(node.module or "").split(".")[0]]
            for module in modules:
                if module in FORBIDDEN_MODULES:
                    raise ScriptSandboxError(
                        f"禁止 import {module}：脚本只能通过 k.<公开op>() 建模、math 算数")
                if module not in ALLOWED_IMPORTS:
                    raise ScriptSandboxError(
                        f"import 不在白名单: {module}（只许 math；几何必须走 k 的公开 op，不提供 build123d）")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ScriptSandboxError(
                    f"禁止访问属性 .{node.attr}：内核内部成员不可触碰，只用 k 的公开 op 方法")
        elif isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in FORBIDDEN_NAMES:
                raise ScriptSandboxError(f"禁止调用 {name}")
    return tree


_SAFE_BUILTIN_NAMES = {
    "print", "len", "range", "enumerate", "zip", "min", "max", "sum", "abs",
    "round", "sorted", "reversed", "list", "dict", "set", "tuple", "str",
    "int", "float", "bool", "isinstance", "issubclass", "repr", "any", "all",
    "map", "filter", "divmod", "pow", "Exception", "ValueError",
    "ZeroDivisionError", "TypeError", "KeyError", "IndexError", "RuntimeError",
    "ArithmeticError", "StopIteration", "True", "False", "None",
}


def safe_builtins() -> dict:
    """受限 __builtins__ 映射：只含纯计算/容器/异常名，无任何 IO/反射能力。

    `__import__` 必须存在（Python import 语句依赖），但换成守卫版：只放行 math，
    与 AST 白名单双保险。
    """
    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or str(name).split(".")[0] not in ALLOWED_IMPORTS:
            raise ImportError(f"脚本沙箱禁止 import: {name}（只许 math）")
        return _builtins.__import__(name, globals, locals, fromlist, level)

    out = {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES
           if hasattr(_builtins, name)}
    out["__import__"] = _guarded_import
    return out


class ScriptKernel:
    """脚本里的 `k`：只暴露 kernel 公开 op（减去黑名单）为同名方法 + 守卫版 execute()。

    v2.16 失败策略（P0-3）：op 返回 success=false 不再被静默吞掉——
    - `failure_policy="abort"`（默认）：任一 op 失败立即抛 ScriptOpError，
      run_script 整体回滚并回传 failed_op（半成品绝不交付）；
    - `failure_policy="best_effort"`：失败被收集进 `failed_ops` 并继续执行，
      run_script 以 success=false + failed_ops 结束（仍不允许静默成功）。

    成功 op 照常返回 StepResult（支持下标：r["success"]）。脚本内如需条件回退，
    用 `try/except Exception` 包住单个调用即可（ScriptOpError 是 RuntimeError 子类）。
    """

    def __init__(self, kernel, allowed_ops, failure_policy: str = "abort") -> None:
        self._kernel = kernel
        self._allowed = frozenset(allowed_ops)
        self._failure_policy = failure_policy
        self.failed_ops: list[dict] = []
        self.op_names = sorted(self._allowed)
        for op in self._allowed:
            method = getattr(kernel, op, None)
            if callable(method):
                setattr(self, op, self._checked(op, method))

    def _checked(self, op: str, method):
        def wrapper(*args, **kwargs):
            result = method(*args, **kwargs)
            if isinstance(result, dict):
                ok = result.get("success")
            else:
                ok = getattr(result, "success", True)
            if ok is False:
                if self._failure_policy == "best_effort":
                    self.failed_ops.append({
                        "op": op,
                        "error_kind": str(getattr(result, "error_kind", None) or
                                          (result.get("error_kind") if isinstance(result, dict) else "") or ""),
                        "error": str(getattr(result, "error", None) or
                                     (result.get("error") if isinstance(result, dict) else "") or ""),
                    })
                else:
                    raise ScriptOpError(op, args, kwargs, result)
            return result
        wrapper.__name__ = op
        return wrapper

    def execute(self, op, **kwargs):
        if op not in self._allowed:
            raise ScriptSandboxError(
                f"op 不在脚本可用集: {op}（公开 op 减去导出/存档黑名单；"
                f"可用示例: {self.op_names[:10]} …）")
        return self._checked(op, lambda **kw: self._kernel.execute(op, **kw))(**kwargs)

    def capabilities(self) -> dict:
        return {"ops": self.op_names,
                "note": "k.<op_name>(**kwargs) 直接调用；返回 StepResult（r['success'] 判断）"}
