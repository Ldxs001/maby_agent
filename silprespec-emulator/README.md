# silprespec-emulator · 前置规范效果模拟器

按方法论（08a 前置规范>后置验证 / 08b 填空的边界 / 08c 槽位的减法 / 09b 穷举一致性）提供 **8 种前置规范方式**，用户根据自己的场景选一种或多种、自由裁剪，对输入**真实执行**（LLM 真填空），观测**填入了什么、重试次数、撑满失败、重现性**。

## 8 种前置规范方式（都是前置，作用在生成通道/填空出口）

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

## 观测结果

- **填入了什么**（实际填空内容——门禁填了哪个词、凝练成什么、槽位填了什么）
- **重试次数**（是否撑满 max_retry）
- **撑满失败次数**
- 命中/留空/编造分布
- **重现性**：并行 N 次各方式跨 run 填入一致率

## 启动

- bat：双击 `setup.bat`（端口 8805）
- 命令行：`python main.py`（默认 LM Studio）、`python main.py --backend ollama`
- 批处理：`python main.py --batch input.json output.json`

后端：LM Studio（默认 `http://localhost:1234`）/ Ollama（`http://localhost:11434`）/ Custom

## 界面

深色主题，三 Tab：配置（方式多选+各方式JSON配置）/ 运行（输入+并行数）/ 结果（填入内容+重试+撑满+重现性）

## 目录

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

## 说明

8 种都是前置规范；后置验证（任务完成后对全量结果的验证）不在本系统。本系统不替用户选方式——用户按自己场景裁剪，系统只管执行并产出可观测结果。
