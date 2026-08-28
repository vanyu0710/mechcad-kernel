"""
Mini pytest 兼容层 - 用于无 pytest 环境
"""
import sys
import os
import importlib
import traceback


class RaisesContext:
    """pytest.raises 上下文管理器（类风格）"""
    def __init__(self, exception_type):
        self.expected = exception_type
        self.value = None
        self.exc_type = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"未抛出异常，期望 {self.expected.__name__}")
        if not issubclass(exc_type, self.expected):
            return False  # 别的异常，让它继续传播
        self.value = exc_val
        self.exc_type = exc_type
        return True  # 吞掉预期异常


def raises(exception_type):
    """pytest.raises 入口"""
    return RaisesContext(exception_type)


def _run_tests(args):
    """简单的测试发现 + 运行器"""
    all_tests = []
    for arg in args:
        if os.path.isdir(arg):
            for root, dirs, files in os.walk(arg):
                for f in sorted(files):
                    if f.startswith("test_") and f.endswith(".py"):
                        full_path = os.path.join(root, f)
                        rel = os.path.relpath(full_path, ".").replace(os.sep, ".")
                        mod_name = rel[:-3]
                        try:
                            mod = importlib.import_module(mod_name)
                            for name in dir(mod):
                                if name.startswith("test_") and callable(getattr(mod, name)):
                                    all_tests.append((mod_name, name, getattr(mod, name)))
                        except Exception as e:
                            print(f"  ✗ 加载 {mod_name} 失败: {e}")
                            traceback.print_exc()
        elif os.path.isfile(arg):
            mod_name = arg[:-3].replace(os.sep, ".")
            mod = importlib.import_module(mod_name)
            for name in dir(mod):
                if name.startswith("test_") and callable(getattr(mod, name)):
                    all_tests.append((mod_name, name, getattr(mod, name)))
    
    print(f"找到 {len(all_tests)} 个测试\n")
    passed = 0
    failed = 0
    failures = []
    for mod_name, name, func in all_tests:
        try:
            func()
            print(f"  ✓ {mod_name}.{name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {mod_name}.{name}: {type(e).__name__}: {e}")
            failed += 1
            failures.append((mod_name, name, f"{type(e).__name__}: {e}"))
    
    print(f"\n通过 {passed}/{len(all_tests)}，失败 {failed}")
    if failures:
        print("\n失败明细:")
        for m, n, e in failures:
            print(f"  - {m}.{n}: {e}")
    return 0 if failed == 0 else 1


def main(args=None):
    """pytest.main 兼容"""
    # v1.16 修复：Windows GBK 控制台打印 ✓/✗ 会 UnicodeEncodeError，强制 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if args is None:
        args = sys.argv[1:] or ["mech_kernel/tests"]
    return _run_tests(args)


__all__ = ["raises", "main"]
