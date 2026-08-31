"""标准化工具接口 ToolSpec + 工具注册

子智能体声明完整的接口契约：输入字段（类型/必填/默认/示例）、输出字段、
引导示例、能力边界。编排器据此匹配前置规范。
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json


@dataclass
class FieldSpec:
    name: str
    type: str = "string"
    required: bool = True
    default: any = None
    description: str = ""
    example: any = None
    options: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type, "required": self.required,
            "default": self.default, "description": self.description,
            "example": self.example, "options": self.options,
        }

    @staticmethod
    def from_dict(d: dict) -> "FieldSpec":
        return FieldSpec(
            name=d.get("name", ""),
            type=d.get("type", "string"),
            required=d.get("required", True),
            default=d.get("default"),
            description=d.get("description", ""),
            example=d.get("example"),
            options=d.get("options", []),
        )


@dataclass
class ExampleSpec:
    title: str = ""
    input: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict:
        return {"title": self.title, "input": self.input,
                "output": self.output, "explanation": self.explanation}

    @staticmethod
    def from_dict(d: dict) -> "ExampleSpec":
        return ExampleSpec(
            title=d.get("title", ""),
            input=d.get("input", {}),
            output=d.get("output", {}),
            explanation=d.get("explanation", ""),
        )


@dataclass
class ToolSpec:
    name: str
    url: str = ""
    endpoint: str = ""
    description: str = ""
    input_fields: list = field(default_factory=list)
    output_fields: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    internal_prespec: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)
    limitations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "url": self.url, "endpoint": self.endpoint,
            "description": self.description,
            "input_fields": [f.to_dict() if hasattr(f, "to_dict") else f for f in self.input_fields],
            "output_fields": [f.to_dict() if hasattr(f, "to_dict") else f for f in self.output_fields],
            "examples": [e.to_dict() if hasattr(e, "to_dict") else e for e in self.examples],
            "internal_prespec": self.internal_prespec,
            "capabilities": self.capabilities,
            "limitations": self.limitations,
        }

    @staticmethod
    def from_dict(d: dict) -> "ToolSpec":
        return ToolSpec(
            name=d.get("name", ""),
            url=d.get("url", ""),
            endpoint=d.get("endpoint", ""),
            description=d.get("description", ""),
            input_fields=[FieldSpec.from_dict(f) if isinstance(f, dict) else f for f in d.get("input_fields", [])],
            output_fields=[FieldSpec.from_dict(f) if isinstance(f, dict) else f for f in d.get("output_fields", [])],
            examples=[ExampleSpec.from_dict(e) if isinstance(e, dict) else e for e in d.get("examples", [])],
            internal_prespec=d.get("internal_prespec", []),
            capabilities=d.get("capabilities", []),
            limitations=d.get("limitations", []),
        )

    @property
    def input_requirements(self) -> list:
        return [f.name + ("?" if not f.required else "") for f in self.input_fields]

    @property
    def output_schema(self) -> list:
        return [f.name for f in self.output_fields]

    def can_accept(self, available_keys: list) -> bool:
        required = [f.name for f in self.input_fields if f.required]
        return all(r in available_keys for r in required)


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec):
    TOOL_REGISTRY[spec.name] = spec


def unregister_tool(name: str):
    TOOL_REGISTRY.pop(name, None)


def get_tool(name: str) -> ToolSpec | None:
    return TOOL_REGISTRY.get(name)


def list_tools() -> list[ToolSpec]:
    return list(TOOL_REGISTRY.values())


def load_tools_from_config(cfg: dict):
    tools_cfg = cfg.get("tools", {})
    for name, spec_dict in tools_cfg.items():
        spec = ToolSpec.from_dict(spec_dict)
        register_tool(spec)


def _init_default_tools():
    register_tool(ToolSpec(
        name="rag-assistant",
        url="http://localhost:8767",
        endpoint="/api/kb/query",
        description="RAG 知识库问答智能体：路由→检索→重排序→NLI验证→生成",
        input_fields=[
            FieldSpec("query", "string", True, None, "用户问题", "茅台酒的制作工艺"),
            FieldSpec("kb", "string", False, None, "知识库名（留空自动路由）", "白酒"),
            FieldSpec("top_k", "int", False, 5, "检索数量", 5),
            FieldSpec("router", "bool", False, True, "启用出库路由", True),
            FieldSpec("reranker", "bool", False, True, "启用重排序", True),
            FieldSpec("nli", "bool", False, False, "启用NLI验证", False),
        ],
        output_fields=[
            FieldSpec("answer", "string", True, None, "生成的回答", ""),
            FieldSpec("docs", "array", True, None, "检索到的文档片段", []),
            FieldSpec("summary", "string", False, None, "回答摘要", ""),
            FieldSpec("sources", "array", False, None, "来源列表", []),
        ],
        examples=[
            ExampleSpec("基础问答", {"query": "茅台酒的制作工艺"}, {},
                        "留空 kb 自动路由到白酒知识库"),
            ExampleSpec("指定知识库", {"query": "量子纠缠是什么", "kb": "量子物理和弦"}, {},
                        "指定 kb 跳过路由直接检索"),
        ],
        internal_prespec=["路由(查询→最佳KB)", "向量检索", "重排序", "NLI验证", "提示词模板"],
        capabilities=["多知识库问答", "自动路由", "重排序优化", "NLI事实校验", "来源追溯"],
        limitations=["需要预建知识库", "不支持跨库join", "数值精度依赖原文"],
    ))
    register_tool(ToolSpec(
        name="structured-writer",
        url="http://localhost:8770",
        endpoint="/api/write",
        description="结构化写作智能体：模板选择→大纲规划→逐段生成→引用后处理",
        input_fields=[
            FieldSpec("topic", "string", True, None, "写作主题", "茅台酒工艺分析"),
            FieldSpec("template", "string", True, "report", "写作模板", "report",
                      options=["report", "article", "essay", "novel"]),
            FieldSpec("material", "string", False, None, "参考材料（文本或文件路径）", ""),
            FieldSpec("kb", "string", False, None, "关联知识库", "白酒"),
            FieldSpec("length", "string", False, "medium", "目标篇幅", "medium",
                      options=["short", "medium", "long"]),
        ],
        output_fields=[
            FieldSpec("article", "string", True, None, "生成的文章", ""),
            FieldSpec("outline", "array", True, None, "大纲结构", []),
            FieldSpec("references", "array", False, None, "引用列表", []),
        ],
        examples=[
            ExampleSpec("报告写作", {"topic": "茅台酒工艺分析", "template": "report"}, {},
                        "生成结构化报告，含大纲+正文+引用"),
            ExampleSpec("关联知识库", {"topic": "白酒品鉴", "template": "article", "kb": "白酒"}, {},
                        "从知识库检索素材后生成文章"),
        ],
        internal_prespec=["模板选择", "大纲规划", "逐段生成", "引用后处理", "RAG素材检索"],
        capabilities=["多模板写作", "大纲规划", "引用追溯", "知识库关联", "篇幅控制"],
        limitations=["依赖模板质量", "长文可能偏离主题", "引用需知识库支持"],
    ))
    register_tool(ToolSpec(
        name="silprespec-emulator",
        url="http://localhost:8789",
        endpoint="/api/emulate",
        description="前置规范效果模拟器：14种组合×真实LLM×验证指标",
        input_fields=[
            FieldSpec("way", "string", True, "pure_guide", "前置规范方式", "pure_guide",
                      options=["pure_guide", "value_bound", "diverge_correct", "deterministic_pin",
                               "detect_report", "custom"]),
            FieldSpec("config", "object", True, None, "方式配置JSON", {}),
            FieldSpec("user_input", "string", True, None, "用户输入", "测试内容"),
        ],
        output_fields=[
            FieldSpec("filled", "object", True, None, "填入内容", {}),
            FieldSpec("success", "bool", True, None, "是否成功", True),
            FieldSpec("metrics", "object", False, None, "验证指标", {}),
        ],
        examples=[
            ExampleSpec("纯软引导", {"way": "pure_guide", "config": {}, "user_input": "AI改变软件开发"}, {},
                        "测试纯引导下LLM填空效果"),
        ],
        internal_prespec=["原子库(10原子)", "Recipe配方", "14种穷举组合", "验证指标"],
        capabilities=["前置规范效果测试", "14种组合穷举", "验证指标量化", "重现性分析"],
        limitations=["仅测试不生产", "需要LLM后端", "指标依赖考题设计"],
    ))


_init_default_tools()
