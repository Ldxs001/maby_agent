# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""配置管理器 — 读写 config.json

配置推动：DEFAULT_CONFIG → config.json → CLI 参数
"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

BACKEND_DEFAULTS = {
    "lm-studio": "http://localhost:1234",
    "ollama": "http://localhost:11434",
    "custom": "",
}

# 界面参数的元数据总表：默认值 / 取值范围 / 步长 / 标签 / 说明。
#
# 这是配置推动链的**最上游**。分两张表，区别只在**是否持久化**：
#
#   PARAM_SPEC — 跨页共享的仪器配置，写入 config.json，DEFAULT_CONFIG.params 由它派生
#   TIME_SPEC  — 时间参数页的单次试算输入，不落盘
#
# 但**取值域一律归本文件管**：界面控件的 min/max/step 全部由这里经 /api/config
# 的 param_spec 下发。任何一处再手写范围或默认值，都会造出第二个源头——那个源头
# 改不动、界面上看不见，只会在自己生效时以诊断文案的形式泄漏给使用者。
#
# kind 取值：
#   float / int — 数值，需 min/max/step（max 为 None 表示不设上限）
#   enum        — 枚举，default 为字符串；options 由各自的权威来源提供
#                 （色谱柱 → COLUMN_TYPES，设备 → DEVICES，模式 → 界面）
PARAM_SPEC = {
    "column_type": {
        "kind": "enum", "default": "c18", "label": "色谱柱",
        "help": "决定排序键：反相 C18/C8/C4、正相、HILIC 按极性指数排序，SEC 按分子量排序。",
    },
    "instrument": {
        "kind": "enum", "default": "qqq", "label": "设备",
        "help": "标称参数来源。标注「实验性」者只指标称参数未充分实测，不改变任何计算。",
    },
    "group_mode": {
        "kind": "enum", "default": "capacity", "label": "分组模式",
        "help": "capacity 由质谱 cycle 反推每组容量；threshold 直接按位置阈值切分。",
    },
    "peak_width_s": {
        "kind": "float", "default": 6.0, "min": 1.0, "max": 30.0, "step": 0.5,
        "label": "峰宽(s)", "help": "色谱峰基线宽度，与点数下限共同决定单个 cycle 的时长上限。",
    },
    "dwell_ms": {
        "kind": "float", "default": 20.0, "min": 1.0, "max": 100.0, "step": 1.0,
        "label": "dwell(ms)", "help": "每个离子对的驻留时间。",
    },
    "delay_q3_ms": {
        "kind": "float", "default": 2.0, "min": 0.0, "max": 20.0, "step": 0.5,
        "label": "Q3切换延迟(ms)", "help": "同一母离子内切换子离子通道时的稳定时间。",
    },
    "delay_q1_ms": {
        "kind": "float", "default": 2.0, "min": 0.0, "max": 20.0, "step": 0.5,
        "label": "Q1切换延迟(ms)", "help": "跨母离子切换时的稳定时间；与 Q3 延迟并行，取较慢者。",
    },
    "overhead_ms": {
        "kind": "float", "default": 2.0, "min": 0.0, "max": 20.0, "step": 0.5,
        "label": "额外开销(ms)", "help": "每个 cycle 的固定开销，含触发与传输。",
    },
    "n_min": {
        "kind": "int", "default": 15, "min": 5, "max": 30, "step": 1,
        "label": "点数下限", "help": "每个色谱峰至少采几个 cycle 点，决定容量的严格程度。",
    },
    "channels_per_compound": {
        "kind": "float", "default": 2, "min": 1.0, "max": 5.0, "step": 0.1,
        "label": "子离子通道/化合物", "help": "每个母离子监测的子离子数（通常定量+定性各一）。",
    },
    "threshold_pct": {
        "kind": "float", "default": 5.0, "min": 1.0, "max": 20.0, "step": 0.5,
        "label": "位置差阈值(%)",
        "help": "threshold 模式的**分组定义本身**：相邻位置差达到此值即开新组。仅该模式生效。",
    },
    "max_span_pct": {
        "kind": "float", "default": 100.0, "min": 0.5, "max": 100.0, "step": 0.5,
        "label": "组内跨度上限(%)",
        "help": "capacity 模式的兜底：一组化合物首尾铺开的宽度上限，超过即开新组。"
                "位置经批次内 min-max 归一化，全批跨度恒为 100%，故上限不可能超过 100%；"
                "取 100 等于不干预，分组完全由容量与质量判据决定。"
                "低于临界值（span_critical_pct）时，这条判据会先于容量判据生效。",
    },
    "fhsw_mode": {
        "kind": "enum", "default": "auto", "label": "质量峰宽来源",
        "help": "auto 用设备标称峰宽；da / ppm 手工指定；custom 用自定义 k 系数。",
    },
    "fhsw_da": {
        "kind": "float", "default": 0.7, "min": 0.001, "max": None, "step": 0.01,
        "label": "FWHM (Da)", "help": "手工指定的质量峰半高全宽，绝对值。",
    },
    "fhsw_ppm": {
        "kind": "float", "default": 20.0, "min": 0.1, "max": None, "step": 1.0,
        "label": "FWHM (ppm)", "help": "手工指定的质量峰半高全宽，相对值。",
    },
    "valley_preset": {
        "kind": "enum", "default": "v10", "label": "谷判据",
        "help": "质量可分辨判据的谷深预设，决定 k 系数。",
    },
    "k_custom": {
        "kind": "float", "default": 2.08, "min": 0.1, "max": None, "step": 0.01,
        "label": "k 系数", "help": "自定义谷判据系数：判据 = k × FWHM。",
    },
}

# 时间参数页（Tab2）的设备专属输入：单机 cycle 模拟参数。
#
# 不落盘——它们是"单次试算的输入"，不是跨页共享的仪器配置。但**取值域仍归此处管**，
# 否则就得在四个设备面板的 HTML 里各手写一份 min/max/step，改一处要翻四处。
#
# 本表只放 Tab2 **独有**的键。与分组页同义的那些物理量——峰宽 / 点数下限 / dwell /
# Q1、Q3 切换延迟 / 额外开销 / 每个化合物的子离子通道数——不在本表重复定义，
# 而是复用 PARAM_SPEC 里的同名项；界面两侧的控件绑定同一个键、双向同步，
# 不再各持一份值。早期 Tab2 的 dwell 与 Tab1 的 dwell 共用一个键名却是两份独立
# 的控件，改哪边都影响不到另一边，也没有任何地方说明它们的关系。
TIME_SPEC = {
    "n_precursors": {
        "kind": "int", "default": 20, "min": 1, "max": 100, "step": 1,
        "label": "母离子数 N₁",
        "help": "一个 cycle 里采集的不同母离子（化合物）总数。"
                "QTRAP 的 MRM 段与阱段共享 cycle 预算，同样时长能容纳的母离子更少。",
    },
    "f_push_khz": {
        "kind": "float", "default": 20, "min": 10, "max": 50, "step": 1,
        "label": "推斥频率(kHz)", "help": "TOF 每秒推斥离子进飞行管的次数。",
    },
    "nsum": {
        "kind": "int", "default": 1000, "min": 100, "max": 10000, "step": 100,
        "label": "叠加次数 nsum", "help": "多少次推斥叠加成一张输出谱。",
    },
    "scan_range_da": {
        "kind": "float", "default": 500, "min": 50, "max": 2000, "step": 50,
        "label": "扫描范围(Da)", "help": "一张谱覆盖的质量区间。",
    },
    "scan_rate_das": {
        "kind": "float", "default": 20000, "min": 10000, "max": 66000, "step": 1000,
        "label": "扫描速率(Da/s)", "help": "阱逐出离子的速度。",
    },
    "fill_ms": {
        "kind": "float", "default": 10, "min": 0.1, "max": 100, "step": 0.1,
        "label": "填充时间(ms)", "help": "阱内积累离子的时长（AGC 自动调节）。",
    },
    "t_switch_ms": {
        "kind": "float", "default": 30, "min": 5, "max": 100, "step": 5,
        "label": "模式切换(ms)", "help": "QTRAP 在 MRM 与阱模式之间往返的开销。",
    },
}

# 界面参数的查询总表：取值不区分是否持久化，一律从这里取。
PARAM_SPEC_ALL = dict(PARAM_SPEC, **TIME_SPEC)

DEFAULT_CONFIG = {
    "llm": {
        "backend": "lm-studio",
        "base_url": "",
        "model": "",
        "api_key": "not-needed",
        "timeout": 120,
        "max_tokens": 4096,
        "temperature": 0.3,
    },
    # params 完全由 PARAM_SPEC 派生，不在此处重复写字面量
    "params": {k: spec["default"] for k, spec in PARAM_SPEC.items()},
}


def default_param(key, default=None):
    """取界面参数的出厂默认值（持久化与非持久化一视同仁）。

    这是配置推动链的**唯一源头**：PARAM_SPEC / TIME_SPEC → DEFAULT_CONFIG →
    config.json → 界面 → 请求 → 计算。下游（含 web_ui）需要兜底值时一律调本函数，
    不要就地写字面量——就地写字面量会造出第二个源头，值一旦分叉，
    界面上看不见也改不了。
    """
    spec = PARAM_SPEC_ALL.get(key)
    if spec is not None:
        return spec["default"]
    return DEFAULT_CONFIG["params"].get(key, default)


def param_spec(key=None):
    """取界面参数的元数据（默认值 / 取值范围 / 步长 / 标签 / 说明）。

    界面据此注入控件的 min/max/step 与缺省值，与 default_param 同源，避免值域在
    HTML 里另写一份。key 为 None 时返回全表（持久化项 + 时间参数页专用项）。

    返回深拷贝，调用方修改不会污染源头。
    """
    if key is None:
        return json.loads(json.dumps(PARAM_SPEC_ALL))
    spec = PARAM_SPEC_ALL.get(key)
    return json.loads(json.dumps(spec)) if spec is not None else None


class ConfigManager:
    def __init__(self, path=None):
        self.path = Path(path) if path else CONFIG_PATH
        self._cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self._merge(saved)
            except Exception:
                pass

    def _merge(self, saved: dict):
        for k, v in saved.items():
            if k in self._cfg and isinstance(self._cfg[k], dict) and isinstance(v, dict):
                self._cfg[k].update(v)
            else:
                self._cfg[k] = v

    def get(self, key, default=None):
        parts = key.split(".")
        cur = self._cfg
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def set(self, key, value):
        parts = key.split(".")
        cur = self._cfg
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        self.save()

    def update(self, data: dict):
        self._merge(data)
        self.save()

    def get_all(self) -> dict:
        return json.loads(json.dumps(self._cfg))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def resolve_base_url(self) -> str:
        backend = self.get("llm.backend", "lm-studio")
        base_url = self.get("llm.base_url", "")
        if not base_url:
            base_url = BACKEND_DEFAULTS.get(backend, "")
        return base_url