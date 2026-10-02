"""插件基类 — 仿 RAG 插件契约：execute(inputs) → {type, content}

输出契约（对齐辅助知识三形态，限定死）：
- type=table  → content 为 CSV 文本（写作时转临时文件走 select_table 蓝皮书取数）
- type=text   → content 为纯文本（直接注入）
- type=image  → content 为图片路径（v1 插件暂不产出）

所有设置都在插件内部完成；消费方（web_ui）只负责：
按 input_fields 收集参数 → execute() → 按 output_types 接数据。
"""


class BasePlugin:
    """插件基类。子类必须实现 execute()。"""

    # 唯一 id（plugin.json 的 name 一致）
    id = ""
    # 展示名 / 描述
    name = ""
    desc = ""
    # 参数表单声明：[{key, label, type: text|password|select, options?, required, hint?}]
    input_fields = []
    # 输出类型声明（对齐三形态）
    output_types = ["table"]

    def __init__(self, plugin_dir=None):
        self.plugin_dir = plugin_dir

    def execute(self, inputs: dict) -> dict:
        """执行插件。

        inputs: 前端按 input_fields 收集的参数
        返回：
          {"type": "table", "name": "xxx.csv", "content": "<CSV 文本>"}
          或 {"type": "text", "name": "xxx", "content": "..."}
          或 {"error": "错误描述"}
        """
        raise NotImplementedError
