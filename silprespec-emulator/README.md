# silprespec-emulator — 前置规范效果实验台

> 通用实验台：从 **8 种前置规范方式**中选择/裁剪，对输入**真实执行**（LLM 真填空），观测填入内容、重试次数、撑满失败、重现性。不替用户选方式，只管执行并产出可观测结果。
>
> 版本：0.3.2b3 | 作者：wUwproject | 许可证：Apache 2.0

---

## 目录

- [一、它是什么](#一它是什么)
- [二、环境要求与依赖](#二环境要求与依赖)
- [三、启动](#三启动)
- [四、命令行参数](#四命令行参数)
- [五、8 种前置规范方式](#五8-种前置规范方式)
- [六、观测结果](#六观测结果)
- [七、配置详解](#七配置详解)
- [八、界面](#八界面)
- [九、目录结构](#九目录结构)
- [十、边界说明](#十边界说明)

---

## 一、它是什么

按方法论（08a 前置规范>后置验证 / 08b 填空的边界 / 08c 槽位的减法 / 09b 穷举一致性）设计的前置规范效果实验台。用户根据自己的场景选择一种或多种前置规范方式、自由裁剪配置，对输入真实执行（LLM 真填空），观测每种方式填入了什么、重试几次、是否撑满失败、并行下重现性如何。

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
| `--e2e` | 一键端到端演示：8 方式 × 预设输入 × 真实 LLM，输出完整原始信息 |
| `--batch INPUT OUTPUT` | 批处理：输入 JSON → 输出 JSON |
| `--jsonl` | 批处理输入按 JSONL 逐行处理 |
| `--backend` | 后端选择（不传则用 config.json）：lm-studio / ollama / custom |
| `--base-url` | API 地址（覆盖 config） |
| `--api-key` | API Key（覆盖 config） |
| `--model` / `-m` | 模型名称（覆盖 config） |

## 五、8 种前置规范方式

| 方式 | 方法论出处 | 执行 | 观测 |
|---|---|---|---|
| ① 门禁·穷举词组（减法） | 08c论断三/四 | LLM 在每道门禁穷举词里填一个或"未指定"，不 block | 填了哪个词/留空/编造 |
| ② 软引导·引导提示词 | 08a§4 | LLM 在引导下填空 | 填入内容 |
| ③ 凝练+代码固定枚举拼接 | 08c场景二 | LLM 凝练（锚定禁泛化）→ 代码枚举组合 | 凝练成什么/编造数 |
| ④ 槽位限定+查多余编造 | 08c场景一 | LLM 填槽位 → 在填空出口查多余编造 | 各槽位填入/多余编造 |
| ⑤ 发散+确定性纠偏 | 08c场景三 | LLM 发散生成 → 代码纠偏 | 纠偏前后 |
| ⑥ 确定性后处理（完全封死） | 08a§4.3 | LLM 生成 → 代码钉死 | 钉死后内容 |
| ⑦ 检出即上报 | 08a§7 | LLM 生成 → 检出+标记+上报人工 | 内容+检出标记 |
| ⑧ required最小化 | 08c§4.3 | required 槽必填，可留空槽留空 | 填入/留空数 |

**空坐标形态**（每种可配）：点对点 / 点对面 / 面对面

## 六、观测结果

- **填入了什么**（实际填空内容——门禁填了哪个词、凝练成什么、槽位填了什么）
- **重试次数**（是否撑满 max_retry）
- **撑满失败次数**
- 命中/留空/编造分布
- **重现性**：并行 N 次各方式跨 run 填入一致率

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

深色主题，三 Tab：**配置**（方式多选 + 各方式 JSON 配置）/ **运行**（输入 + 并行数）/ **结果**（填入内容 + 重试 + 撑满 + 重现性）。

## 九、目录结构

```
silprespec-emulator/
├── main.py / setup.bat / requirements.txt
├── config.json             # 持久化配置（配置推动）
├── data/experiment.json
└── silprespec_emulator/
    ├── config_manager.py   # ConfigManager + DEFAULT_CONFIG（配置推动）
    ├── pipeline_model.py   # 8种方式配置 + 结果模型 + 重现性
    ├── simulator.py        # 执行引擎 + 8种方式真实执行器
    ├── llm_client.py       # 多后端 LLM
    └── web_ui.py
```

## 十、边界说明

- 8 种都是前置规范；**后置验证**（任务完成后对全量结果的验证）不在本系统
- 本系统不替用户选方式——用户按自己场景裁剪，系统只管执行并产出可观测结果
