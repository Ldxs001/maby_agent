# silprespec-emulator — 前置规范效果实验台

> 通用实验台：从 **5 种前置规范方式**中选择/组合，对输入**真实执行**（LLM 真填空），观测填入内容、重试次数、撑满失败、重现性 + **验证指标**（量化每种后置是否真的生效）。不替用户选方式，只管执行并产出可观测结果。
>
> 版本：0.5.0b1 | 作者：wUwproject | 许可证：Apache 2.0

---

## 目录

- [一、它是什么](#一它是什么)
- [二、环境要求与依赖](#二环境要求与依赖)
- [三、启动](#三启动)
- [四、命令行参数](#四命令行参数)
- [五、5 种前置规范方式](#五5-种前置规范方式)
- [六、观测结果与验证指标](#六观测结果与验证指标)
- [七、配置详解](#七配置详解)
- [八、界面](#八界面)
- [九、目录结构](#九目录结构)
- [十、边界说明](#十边界说明)

---

## 一、它是什么

按方法论（08a 前置规范>后置验证 / 08b 填空的边界 / 08c 槽位的减法 / 09b 穷举一致性）设计的前置规范效果实验台。**软引导（任务提示词）是第一位基础原子**，所有方式建立在它之上。用户根据自己的场景选择一种或多种前置规范方式、自由裁剪配置，对输入真实执行（LLM 真填空），观测每种方式填入了什么、重试几次、是否撑满失败、并行下重现性如何，以及**验证指标**量化每种后置是否真的生效。

## 二、环境要求与依赖

- **Python 3.11+**（`setup.bat` 启动时检测）
- **仅标准库**（`http.server` / `threading` / `json` / `urllib`），**无第三方 pip 依赖**（见 `requirements.txt`）
- **LLM 由外部后端服务提供**：LM Studio（默认 `http://localhost:1234`）/ Ollama（`http://localhost:11434`）/ 任意 OpenAI 兼容 API

## 三、启动

- **一键启动（bat）**：双击 `setup.bat`（端口 8805）
- **命令行（Web UI，默认行为）**：`python main.py`（默认 LM Studio）、`python main.py --backend ollama`
- **批处理**：`python main.py --batch input.json output.json`

## 四、命令行参数

| 参数 | 说明 |
|---|---|
| `--web` | 启动 Web UI（默认行为） |
| `--port` | 监听端口（默认 8805） |
| `--host` | 监听地址（默认 0.0.0.0） |
| `--pidfile` | PID 文件路径（setup.bat 用） |
| `--check` | 仅检测后端连接，不进入对话 |
| `--e2e` | 一键端到端演示：5 方式 × 预设输入 × 真实 LLM，输出完整原始信息 |
| `--batch INPUT OUTPUT` | 批处理：输入 JSON → 输出 JSON |
| `--jsonl` | 批处理输入按 JSONL 逐行处理 |
| `--backend` | 后端选择（不传则用 config.json）：lm-studio / ollama / custom |
| `--base-url` | API 地址（覆盖 config） |
| `--api-key` | API Key（覆盖 config） |
| `--model` / `-m` | 模型名称（覆盖 config） |

## 五、5 种前置规范方式

| 方式 | 方法论出处 | 执行 | 观测 |
|---|---|---|---|
| ① 纯软引导 | 08a§4 | 只任务提示词，LLM 自由填空，可加输出约束校验 | 填入内容/达标比例 |
| ② 值域限定 | 08c论断三/四+场景一/二+§4.3 | bound_type 区分：可枚举选择/槽位提取/必填最小化/凝练+枚举过滤 | 值域命中率/编造检出率/重试回值域率 |
| ③ 发散纠偏 | 08c场景三 | LLM 高温度发散 → 代码确定性纠偏（语义偏离拉回） | changed/纠偏编辑距离/纠偏有效性/达标比例 |
| ④ 确定性封死 | 08a§7 A 形态 | LLM 生成 → 代码钉死可枚举（错误无通道） | changed/达标率/多次100%完全一致 |
| ⑤ 检出上报 | 08a§7 B 形态 | LLM 生成 → 检出+标记+上报人工（不阻塞） | 检出率/上报率 |
| + 自定义组合 | — | 自由组合原子，A 与 B 互斥，其余任意组合 | 取决于选的原子 |

**组合规则**：值域限定(A) 与 发散纠偏(B) 互斥（收敛 vs 放开），其余任意组合。

## 六、观测结果与验证指标

- **填入了什么**（实际填空内容）
- **重试次数**（是否撑满 max_retry）
- **撑满失败次数**
- **重现性**：并行 N 次各方式跨 run 填入一致率
- **验证指标**（量化每种后置是否真的生效）：
  - 纯软引导：达标比例
  - 值域限定：值域命中率 + 编造检出率 + 重试回值域率
  - 发散纠偏：changed 比例 + **纠偏编辑距离**(Levenshtein) + **纠偏有效性**(raw不达标→corrected达标) + 达标比例
  - 确定性封死：changed 比例 + 达标率 + **多次 100% 完全一致**（代码零采样）
  - 检出上报：检出率 + 上报率

## 七、配置详解

配置优先级：**代码级默认 < `config.json` 用户持久化 < 命令行参数**（LLM 创建永远从 config 读取，不硬编码）。

`config.json` 结构（代码级默认值，见 `config_manager.py` 的 `DEFAULT_CONFIG`）：

```json
{
  "llm": {
    "backend": "lm-studio",
    "base_url": "",
    "model": "",
    "api_key": "not-needed",
    "timeout": 120,
    "max_tokens": 4096,
    "temperature": 0.7
  },
  "parallel": 5,
  "custom_templates": []
}
```

后端默认地址（`BACKEND_DEFAULTS`）：`lm-studio` → `http://localhost:1234`；`ollama` → `http://localhost:11434`；`custom` → 空（需自填 `--base-url` 或 config）。

## 八、界面

深色主题，三 Tab：**配置**（方式多选 + 各方式表单配置 + 软引导任务提示词）/ **运行**（输入 + 并行数）/ **结果**（填入内容 + 重试 + 撑满 + 重现性 + 验证指标）。

## 九、目录结构

```
silprespec-emulator/
├── main.py / setup.bat / requirements.txt
├── config.json             # 持久化配置（配置推动）
├── data/experiment.json
└── silprespec_emulator/
    ├── config_manager.py   # ConfigManager + DEFAULT_CONFIG（配置推动）
    ├── pipeline_model.py   # 5种方式配置 + 结果模型 + 重现性
    ├── atoms.py            # 原子库 + recipe_for + levenshtein + calc_metrics
    ├── simulator.py        # 执行引擎 + 5种方式真实执行器
    ├── llm_client.py       # 多后端 LLM
    └── web_ui.py
```

## 十、边界说明

- 5 种都是前置规范；**后置验证**（任务完成后对全量结果的验证）不在本系统
- 本系统不替用户选方式——用户按自己场景裁剪，系统只管执行并产出可观测结果
- 软引导（任务提示词）是第一位基础原子，所有方式必有
