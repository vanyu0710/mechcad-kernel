"""
MechKernel Capability Registry

v2 升级基础设施：自动注册 + schema 描述 + 参数校验 + 权限分级

专家第 5 轮审查建议：
- 当前 PUBLIC_OPS = frozenset({...}) 硬编码白名单不可维护
- 真实 LLM 需要结构化 op 描述
- 需要权限分级（public / read / internal）
- 需要参数类型/范围/必填校验

设计目标：
- 装饰器自动注册（@cap.register(...)）
- JSON Schema 风格的 input_schema
- LLM 友好的 list_public() 输出
"""
from typing import Any, Dict, List, Optional, Callable, Set, Tuple
from dataclasses import dataclass, field
from functools import wraps
import inspect

# Schema 中支持的类型
SUPPORTED_TYPES = {"string", "number", "integer", "boolean", "tuple", "list", "dict", "enum", "string_or_list"}


@dataclass
class FieldSchema:
    """单个字段的 schema"""
    type: str                       # "string" | "number" | ...
    required: bool = True
    default: Any = None
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    enum: Optional[List] = None      # type="enum" 时用
    items_type: Optional[str] = None  # type="tuple"|"list" 时用
    length: Optional[int] = None     # type="tuple" 时用（2 = (x,y), 3 = (x,y,z)）
    
    def to_dict(self) -> dict:
        d = {"type": self.type, "required": self.required}
        if self.default is not None:
            d["default"] = self.default
        if self.description:
            d["description"] = self.description
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.enum:
            d["enum"] = self.enum
        if self.items_type:
            d["items_type"] = self.items_type
        if self.length:
            d["length"] = self.length
        return d


@dataclass
class Capability:
    """一个能力的完整描述"""
    name: str
    category: str                          # "sketch" | "body" | "detail" | "query" | "edit" | "control"
    description: str                       # 给 LLM 看
    input_schema: Dict[str, FieldSchema] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    permission: str = "public"             # "public" | "read" | "internal" | "experimental"
    func: Optional[Callable] = None
    examples: List[Dict] = field(default_factory=list)  # Few-shot 用

    def to_llm_dict(self) -> dict:
        """LLM 友好的 op 描述"""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "permission": self.permission,
            "experimental": self.permission == "experimental",
            "inputs": {k: v.to_dict() for k, v in self.input_schema.items()},
            "outputs": self.output_schema,
            "examples": self.examples,
        }
    
    def validate_inputs(self, kwargs: dict) -> Tuple[bool, str]:
        """参数校验：返回 (is_valid, error_message)"""
        for fname, fschema in self.input_schema.items():
            if fname not in kwargs:
                if fschema.required:
                    return False, f"{fname} is required (missing)"
                continue
            value = kwargs[fname]
            if value is None and fschema.required:
                return False, f"{fname} is required (got None)"
        
        for fname, value in kwargs.items():
            if fname not in self.input_schema:
                return False, f"unknown field: {fname}"
            
            fschema = self.input_schema[fname]
            ok, err = self._validate_field(fname, value, fschema)
            if not ok:
                return False, err
        
        return True, ""
    
    def _validate_field(self, name: str, value: Any, schema: "FieldSchema") -> Tuple[bool, str]:
        """校验单个字段"""
        # 必填检查
        if value is None and schema.required:
            return False, f"{name} is required but got None"
        
        if value is None and not schema.required:
            return True, ""
        
        # 类型检查
        t = schema.type
        if t == "string":
            if not isinstance(value, str):
                return False, f"{name} must be string, got {type(value).__name__}"
        elif t == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False, f"{name} must be number, got {type(value).__name__}"
            if schema.min is not None and value < schema.min:
                return False, f"{name}={value} < min {schema.min}"
            if schema.max is not None and value > schema.max:
                return False, f"{name}={value} > max {schema.max}"
        elif t == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return False, f"{name} must be integer, got {type(value).__name__}"
            if schema.min is not None and value < schema.min:
                return False, f"{name}={value} < min {schema.min}"
        elif t == "boolean":
            if not isinstance(value, bool):
                return False, f"{name} must be boolean, got {type(value).__name__}"
        elif t == "enum":
            if schema.enum and value not in schema.enum:
                return False, f"{name}={value} not in enum {schema.enum}"
        elif t == "tuple":
            if not isinstance(value, (tuple, list)):
                return False, f"{name} must be tuple/list, got {type(value).__name__}"
            if schema.length and len(value) != schema.length:
                return False, f"{name} length {len(value)} != {schema.length}"
            if schema.items_type == "number":
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
                    return False, f"{name} items must be numbers"
        elif t == "list":
            if not isinstance(value, list):
                return False, f"{name} must be list, got {type(value).__name__}"
        elif t == "string_or_list":
            # v2.11: 'all' 或引用列表（如 fillet edges）
            if isinstance(value, str):
                pass
            elif isinstance(value, (list, tuple)):
                if not all(isinstance(x, str) for x in value):
                    return False, f"{name} items must be strings"
            else:
                return False, f"{name} must be string or list of strings, got {type(value).__name__}"
        elif t == "dict":
            if not isinstance(value, dict):
                return False, f"{name} must be dict, got {type(value).__name__}"
        
        return True, ""


class CapabilityRegistry:
    """
    能力注册表（自动 + 装饰器 + LLM 友好）。
    
    用法：
        cap = CapabilityRegistry()
        
        @cap.register(
            name="add_circle",
            category="sketch",
            description="在草图中添加圆",
            inputs={
                "sketch_name": FieldSchema(type="string", required=True),
                "center": FieldSchema(type="tuple", length=2, items_type="number", required=True),
                "radius": FieldSchema(type="number", required=True, min=0.001),
            },
        )
        def add_circle(sketch_name, center, radius):
            ...
    
    LLM 端：
        public_ops = cap.list_public()  # 返回 LLM 用的 op 描述
    """

    def set_capability(self, cap: 'Capability', overwrite: bool = False) -> None:
        """P1-4 修复(v8): 直接设置 capability（带重复警告）"""
        import warnings
        if cap.name in self._caps and not overwrite:
            warnings.warn(
                f"capability '{cap.name}' 已被注册（覆盖可能丢失 schema/func）",
                UserWarning,
                stacklevel=2,
            )
        self._caps[cap.name] = cap

    def __init__(self):
        self._caps: Dict[str, Capability] = {}
        self._call_count: int = 0
    
    def register(
        self,
        name: str,
        category: str,
        description: str,
        inputs: Optional[Dict[str, FieldSchema]] = None,
        outputs: Optional[Dict] = None,
        permission: str = "public",
        examples: Optional[List[Dict]] = None,
    ):
        """
        装饰器：注册一个能力。
        
        Args:
            name: op 名（如 "add_circle"）
            category: 分类（"sketch" | "body" | "detail" | "query" | "edit"）
            description: 人类可读描述
            inputs: 输入字段 schema
            outputs: 输出 schema
            permission: "public" | "read" | "internal"
            examples: Few-shot 例子
        """
        def decorator(func: Callable) -> Callable:
            # 校验：name 不能以下划线开头（除非 internal）
            if name.startswith("_") and permission != "internal":
                raise ValueError(f"op 名 {name} 以下划线开头，必须 permission='internal'")
            
            # P1-4 修复（v8 DeepSeek）：重复注册警告
            if name in self._caps:
                import warnings
                warnings.warn(
                    f"capability '{name}' 重复注册。已覆盖旧 schema（可能是不一致的 op 重复定义）",
                    UserWarning,
                    stacklevel=2,
                )
            
            cap = Capability(
                name=name,
                category=category,
                description=description,
                input_schema=inputs or {},
                output_schema=outputs or {"type": "StepResult"},
                permission=permission,
                func=func,
                examples=examples or [],
            )
            self._caps[name] = cap
            return func
        return decorator
    
    def get(self, name: str) -> Optional[Capability]:
        """获取一个 capability"""
        return self._caps.get(name)
    
    def has(self, name: str) -> bool:
        return name in self._caps
    
    def list_public(self) -> List[dict]:
        """列出所有 public 权限的 op（LLM 友好；experimental 不在此列）"""
        return [
            cap.to_llm_dict()
            for cap in self._caps.values()
            if cap.permission == "public"
        ]

    def list_experimental(self) -> List[dict]:
        """v2.11: 列出 experimental op（默认能力集之外，显式 allow_experimental 才能调）"""
        return [
            cap.to_llm_dict()
            for cap in self._caps.values()
            if cap.permission == "experimental"
        ]
    
    def list_by_category(self, category: str) -> List[dict]:
        """按分类列出"""
        return [
            cap.to_llm_dict()
            for cap in self._caps.values()
            if cap.category == category and cap.permission == "public"
        ]
    
    def list_all(self) -> List[dict]:
        """列出所有 op（调试用）"""
        return [cap.to_llm_dict() for cap in self._caps.values()]
    
    def validate_call(self, name: str, kwargs: dict, allow_experimental: bool = False) -> Tuple[bool, str]:
        """校验一次 op 调用

        v2.11: experimental op 默认拒绝；allow_experimental=True 时放行并正常校验参数。
        """
        cap = self.get(name)
        if cap is None:
            return False, f"未知 op: {name}"
        if cap.permission != "public":
            if cap.permission == "experimental" and allow_experimental:
                return cap.validate_inputs(kwargs)
            return False, f"op {name} 权限是 {cap.permission}，不允许调用"
        return cap.validate_inputs(kwargs)
    
    def call(self, name: str, **kwargs) -> Any:
        """
        统一调用入口（带校验 + 异常隔离）。
        
        P0-1 修复：func 异常不再逃逸到 caller，返回错误 dict
        
        Raises:
            ValueError: 未知 op / 权限不足 / 参数校验失败
        """
        self._call_count += 1
        
        ok, err = self.validate_call(name, kwargs)
        if not ok:
            raise ValueError(err)
        
        cap = self.get(name)
        try:
            return cap.func(**kwargs)
        except Exception as e:
            # P0-1: 异常隔离（func bug 不应逃逸）
            return {
                "success": False,
                "error": f"func raised: {type(e).__name__}: {e}",
                "error_kind": "KERNEL_BUG",
                "op": name,
            }
    
    @property
    def call_count(self) -> int:
        return self._call_count
    
    def __len__(self) -> int:
        return len(self._caps)
    
    def __contains__(self, name: str) -> bool:
        return self.has(name)


# 便捷：构造 FieldSchema 的工厂函数
def string(required: bool = True, default: Optional[str] = None, description: str = "", enum: Optional[List] = None) -> FieldSchema:
    return FieldSchema(type="string" if not enum else "enum", required=required, default=default, description=description, enum=enum)


def number(required: bool = True, default: Optional[float] = None, min: Optional[float] = None, max: Optional[float] = None, description: str = "") -> FieldSchema:
    return FieldSchema(type="number", required=required, default=default, min=min, max=max, description=description)


def integer(required: bool = True, default: Optional[int] = None, min: Optional[int] = None, max: Optional[int] = None, description: str = "") -> FieldSchema:
    return FieldSchema(type="integer", required=required, default=default, min=min, max=max, description=description)


def boolean(required: bool = True, default: Optional[bool] = None, description: str = "") -> FieldSchema:
    return FieldSchema(type="boolean", required=required, default=default, description=description)


def enum_field(values: List, required: bool = True, default: Any = None, description: str = "") -> FieldSchema:
    return FieldSchema(type="enum", required=required, default=default, enum=values, description=description)


def tuple2(items_type: str = "number", required: bool = True, description: str = "") -> FieldSchema:
    return FieldSchema(type="tuple", required=required, items_type=items_type, length=2, description=description)


def tuple3(items_type: str = "number", required: bool = True, description: str = "") -> FieldSchema:
    return FieldSchema(type="tuple", required=required, items_type=items_type, length=3, description=description)


def list_of(items_type: str = "string", required: bool = True, description: str = "") -> FieldSchema:
    return FieldSchema(type="list", required=required, items_type=items_type, description=description)
