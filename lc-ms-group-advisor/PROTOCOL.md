# LC-MS 分组顾问 Protocol

> 版本: 0.1.0b0 | 作者: wUwproject | 许可证: Apache 2.0
> 更新: 2026-09-10

---

## 1. 概述

LC-MS 分组顾问把「化合物清单」翻译成「质谱采集策略」：预测液相色谱出峰顺序，
按位置与质量两条硬约束分组，并对每组做 MRM cycle 核算与达标判定。

### 技术栈

- Python 3.11+ 标准库（**零第三方依赖**）
- HTTP 服务器：`http.server`（内置）
- LLM 通信：`urllib`（OpenAI 兼容 API）
- 前端：纯 HTML/CSS/JS 内联（无构建步骤、无外部 CDN）

### 三个计算域

| Tab | 输入 | 输出 |
|---|---|---|
| 分组预测 | 化合物（名称 / 化学式 / 母离子 m/z）+ 色谱参数 + 质谱参数 | 出峰顺序、分组方案与开组原因、每组 cycle/点数/判定、自检告警 |
| 时间参数 | 单台设备参数 | cycle 构成明细、甘特图分段、点数 |
| 通量核算 | 离子对列表 | 邻近性检查、三点判定、反推上限 |

---

## 2. CLI 参数

```
python main.py [OPTIONS]
```

| 参数 | 说明 |
|------|------|
| `--port PORT` | Web UI 端口（默认 8810；传 `auto` 自动选空闲端口） |
| `--host HOST` | 监听地址（默认 `0.0.0.0`） |
| `--pidfile PATH` | PID 文件路径（`setup.bat` 用于停服） |
| `--check` | 仅检测 LLM 后端连接并退出，不启动服务 |
| `--backend {lm-studio,ollama,custom}` | LLM 后端（覆盖 config.json） |
| `--base-url URL` | API 地址（覆盖 config.json） |
| `--api-key KEY` | API Key（覆盖 config.json） |
| `--model NAME` / `-m NAME` | 模型名称（覆盖 config.json） |

配置优先级：**命令行参数 > `config.json` > 代码级默认值**。

---

## 3. HTTP API

所有接口返回 JSON（`application/json; charset=utf-8`）。业务错误通过响应体的
`ok: false` + `error` 字段返回，HTTP 状态码仍为 200；仅未知路由返回 404。

### 3.1 GET

| 路由 | 功能 |
|------|------|
| `/` `/index.html` | Web UI 主页面（HTML） |
| `/api/config` | 返回全部配置 + `param_spec`（界面参数元数据：`kind` / `default` / `min` / `max` / `step` / `label` / `help`；含时间参数页专用项） |
| `/api/columns` | 柱型表：`{key: {label, note, key, key_label}}` |
| `/api/devices` | 设备表：`{key: {label, dwell_min_ms, n_min, res_note, res_da, res_ppm}}` |
| `/api/res_presets` | 判据预设（谷值预设、峰宽模式、默认值、k 对照表） |
| `/api/backends` | `{backends, current, base_url, model}` |
| `/api/llm/models?backend=&base_url=` | `{success, models, error?}` |

### 3.2 POST

| 路由 | 功能 |
|------|------|
| `/api/config` | 合并写入配置（部分键更新） |
| `/api/backend` | 更新 LLM 后端设置 |
| `/api/backend/test` | 测试后端连通性 |
| `/api/extract` | LLM 锚点指针抽取 |
| `/api/separate` | 分组预测（分离 + 分组 + 质谱核算） |
| `/api/time_sim` | 单设备时间参数模拟 |
| `/api/flux_check` | 通量核算 |

**设备标注**：`/api/devices` 返回的 `label` 中，`qqq`（三重四极杆 QqQ）为正式支持设备；`it` / `qtrap` / `qtof` 的 label 带「（实验性）」后缀，表示该设备的标称参数与界面标注尚未经充分实测验证。后缀只作用于展示——`device` / `device_label` 字段会原样带上它，但所有设备共用同一套 cycle、点数下限与质量判据逻辑，接口字段与判定行为不因设备而异。

---

## 4. 数据结构

### 4.1 POST /api/backend

**请求**

```json
{ "backend": "lm-studio", "base_url": "http://127.0.0.1:1234",
  "model": "qwen3-8b", "api_key": "not-needed" }
```

**响应**：`{"ok": true}`

### 4.2 POST /api/backend/test

**请求**：同 `/api/backend`
**响应**：`{"ok": true, "message": "连接成功，模型列表：..."}`

### 4.3 POST /api/extract

**请求**

```json
{ "text": "二甲双胍 C4H11N5 130.1 71.1\n阿卡波糖 C25H43NO18 646.2" }
```

**响应**

```json
{
  "ok": true,
  "mode": "anchors",
  "compounds": [
    {"name": "二甲双胍", "formula": "C4H11N5", "precursor": 130.1,
     "product_quant": 71.1, "product_qual": null}
  ],
  "failures": [],
  "missing": {"formula": [], "precursor": []},
  "warnings": [],
  "error": null
}
```

| 字段 | 含义 |
|------|------|
| `mode` | 实际使用的协议模式（`anchors` / `columns`） |
| `compounds` | 抽取值，已由 Python 从原文取出并类型化 |
| `failures` | 未命中 / 乱序 / 编号断裂的条目清单（含原因） |
| `missing` | 按槽位归类的缺失清单；`formula` 非空表示整批不成立 |
| `ok` | `false` 时 `error` 给出可读原因（含未命中回灌后的失败清单） |

**闸门语义**：三条硬闸门（引用命中 / 位置有序 / 编号连续）任一不过即 `ok: false`；
`formula` 缺失为**批次级阻塞**（分组排序的唯一输入缺失，不能局部剔条目）。

### 4.4 POST /api/separate

**请求**

```json
{
  "compounds": [
    {"name": "阿司匹林", "formula": "C9H8O4", "precursor": 181.05}
  ],
  "params": {
    "column_type": "c18", "instrument": "qqq",
    "group_mode": "capacity", "threshold_pct": 5, "max_span_pct": 100,
    "peak_width_s": 6, "n_min": 15,
    "dwell_ms": 20, "delay_q1_ms": 2, "delay_q3_ms": 2, "overhead_ms": 2,
    "channels_per_compound": 2,
    "fhsw_mode": "auto", "fhsw_da": 0.7, "fhsw_ppm": 20,
    "valley_preset": "v10", "k_custom": 2.08
  }
}
```

**响应**

```json
{
  "ok": true,
  "separation": {
    "items": [{"idx": 0, "name": "阿司匹林", "formula": "C9H8O4",
               "pi": 1.3721, "mass": 180.0423, "position_pct": 33.33,
               "sort_key": "pi", "sort_value": 1.3721, "sort_value_valid": true,
               "precursor": 181.05, "polarity_desc": "中等极性"}],
    "column": {"type": "c18", "label": "反相 C18/C8/C4", "note": "...",
               "key": "pi", "key_label": "极性指数"},
    "groups": [{"group_id": 1, "range": [0.0, 12.5],
                "compounds": ["阿司匹林"], "n_compounds": 1, "members": [...]}],
    "group_mode": "capacity",
    "max_compounds_per_group": 4,
    "threshold_pct": null,
    "max_span_pct": 100.0,
    "span_critical_pct": 68.5,
    "mass_conflicts": [{"a": 379.0678, "b": 380.0676, "dm": 0.9998,
                        "fhsw_da": 0.7, "k": 2.0789, "need_da": 1.4552,
                        "valley_pct": 48.5, "overlap": true,
                        "a_name": "甲", "b_name": "乙", "pos_gap": 2.5}],
    "split_reasons": [{"after_group": 1, "after": "甲", "before": "乙",
                       "reason": "mass", "detail": {"dm": 0.9998, "need_da": 1.4552}}],
    "warnings": [{"level": "warn", "code": "missing_formula", "message": "..."}]
  },
  "ms_eval": {
    "device": "三重四极杆 QqQ", "instrument": "qqq",
    "groups": [{"group": {...}, "eval": {...}, "device": "三重四极杆 QqQ"}],
    "summary": {"n_total_compounds": 5, "n_groups": 2, "n_pass": 2, "n_fail": 0,
                "without_grouping": {"n_transitions": 10, "cycle_ms": 220.0,
                                     "n_points": 27.3, "would_fail": false}}
  },
  "res": {"k": 2.0789, "valley_pct": 10.0, "valley_preset": "v10",
          "fhsw_mode": "auto", "fhsw_value": 0.7, "fhsw_unit": "Da（设备标称，恒定）"}
}
```

**关键字段**

| 字段 | 含义 |
|------|------|
| `items[].idx` | 输入列表原始序号——重名化合物与排序后位置的唯一稳定标识 |
| `items[].sort_key` | 本柱型的排序键（`pi` / `mass`） |
| `items[].sort_value_valid` | 排序键是否由有效分子式算出（假 = 占位值） |
| `split_reasons[].reason` | 开新组原因：`mass`（质量不可分辨）/ `capacity`（容量上限）/ `span`（`capacity` 模式：组内跨度过宽）/ `gap`（`threshold` 模式：相邻位置差达阈值） |
| `split_reasons[].detail.op` | 位置判据记录专用：本次比较实际用的运算符（`gap` 为 `>=`，`span` 为 `>`），供界面如实渲染，不靠猜 |
| `split_reasons[].detail.head` / `tail` | `span` 记录专用：该组首尾化合物名——跨度就是这两者之间铺开的宽度 |
| `max_span_pct` | `capacity` 模式生效的组内跨度上限（`threshold` 模式为 `null`）；与请求参数同名同值 |
| `span_critical_pct` | 完全不触发跨度判据所需的最小跨度上限，由该批次实际分组中各组的最大跨度算出（`threshold` 模式为 `null`）。设值低于它，跨度判据会抢在容量之前拆组，「质谱能力反推」名不副实 |
| `mass_conflicts[].pos_gap` | 冲突双方的出峰位置差——用于判断能否靠时间调度解决 |
| `eval.verdict` | `pass` / `fail_points` / `fail_dwell` |
| `eval.suggestion` | 不达标时的可执行建议（如「每组不超过 N 个化合物」） |
| `summary.without_grouping` | 不分组反事实对照，用于展示分组收益 |

**分组优先级**：质量冲突 > 容量上限 > 位置判据。位置判据的对象随模式而异——`capacity` 模式是「组内跨度 > `max_span_pct`」，只看一组首尾铺多宽（整体量）；`threshold` 模式是「相邻差 >= `threshold_pct`」，逐对比较（局部量）。`capacity` 下 `max_span_pct` 取 100 即不干预：位置经批次内 min-max 归一化，全批跨度恒为 100%，而一组是批次的子集，上限不可能更高。

**自检告警代码**：`missing_formula` / `out_of_domain` / `sec_under_domain` / `multi_charge`。

### 4.5 POST /api/time_sim

**请求**（按设备取用对应字段）

```json
{ "device": "qqq", "peak_width_s": 6, "n_min": 15,
  "n_precursors": 5, "n_products": 3.4,
  "dwell_ms": 20, "delay_q3_ms": 2, "delay_q1_ms": 2, "overhead_ms": 2 }
```

Q-TOF 用 `f_push_khz` / `nsum`；离子阱用 `scan_range_da` / `scan_rate_das` / `fill_ms`；
QTRAP 用 MRM 段参数 + 阱段参数 + `t_switch_ms`。

`peak_width_s` / `n_min` / `dwell_ms` / `delay_q1_ms` / `delay_q3_ms` / `overhead_ms` 与分组预测、
通量核算两页共用同一份配置（界面控件绑定同一参数键，任一改动即同步其余并写回 `config.json`）；
`n_products` 取自共用的「子离子通道/化合物」。其余字段为时间参数页专用，不写入配置文件。
服务端一律只按请求读数，不自行回落配置。

**响应**

```json
{
  "ok": true, "device": "qqq", "device_label": "三重四极杆 QqQ",
  "axes": {"t_cycle_ms": 378.0, "n_points": 15.9, "duty": 34.5, "ok": true},
  "formula": "T_cycle = 5×3.4×20 + 12×2(Q3切) + 4×3(Q1/Q3并行切) + 2 = 378.0 ms",
  "segments": [{"label": "化合物1 子1", "ms": 20, "kind": "dwell"}],
  "omitted_ms": 0.0, "peak_width_s": 6.0, "n_min": 15, "help": "..."
}
```

| 字段 | 含义 |
|------|------|
| `axes.t_cycle_ms` | 单 cycle 时间（ms） |
| `axes.n_points` | 色谱峰上的采集点数 |
| `axes.duty` | 占空比（%） |
| `segments` | 甘特图分段，各段 `ms` 之和恒等于 `t_cycle_ms` |
| `omitted_ms` | 省略段合计耗时（化合物过多时中间段聚合显示） |

**注**：`n_products` 可填小数（等效平均值），计算用浮点；甘特图渲染时取整。
段宽 `flex` 严格正比于 `ms`，因此渲染填充率恒为 100%。

### 4.6 POST /api/flux_check

**请求**

```json
{ "compounds": [{"name": "甲", "precursor": 379.0678,
                 "product_quant": 174.05, "product_qual": 132.04}],
  "params": {"instrument": "qqq", "peak_width_s": 6, "n_min": 15, "dwell_ms": 20,
             "channels_per_compound": 2, "valley_preset": "v10"} }
```

**响应**

```json
{
  "ok": true,
  "N1": 3, "N3": 2, "n_compounds": 3, "n_qual": 3,
  "axes": {"t_cycle_ms": 150.0, "n_points": 40.0},
  "checks": [
    {"key": "points", "ok": true, "msg": "点数 40.0 ≥ 15，达标"},
    {"key": "dwell",  "ok": true, "msg": "dwell 20ms ≥ 设备下限 2.0ms，仪器做得到"},
    {"key": "mass",   "ok": false, "msg": "母离子 379.0678 vs 380.0676：Δm=0.9998Da < 判据 1.4552Da（= 2.0789×FWHM 0.7Da），质量混峰、谷值仅 48.5%"}
  ],
  "prox": {
    "precursor": {"a": 379.0678, "b": 380.0676, "kind": "precursor",
                  "dm": 0.9998, "fhsw_da": 0.7, "k": 2.0789,
                  "need_da": 1.4552, "valley_pct": 48.5, "overlap": true},
    "product": null
  },
  "res": {"k": 2.0789, "valley_pct": 10.0, "valley_preset": "v10"},
  "backderive": {"w_min_s": 1.13, "max_precursors": 18, "dwell_max_ms": 66.67,
                 "dwell_min_ms": 2.0, "dwell_ok_range": "2.0–66.67 ms"}
}
```

| 字段 | 含义 |
|------|------|
| `prox.precursor` / `prox.product` | 母离子间 / 子离子间的最近邻对及其判据结果；无邻近风险时为 `null` |
| `backderive` | 反推上限：满足点数下限所需的最小峰宽、最大母离子数、dwell 可用范围 |

---

## 5. 内部判据入口

所有质量判定必须经由 `mass_resolution.py`，禁止在调用点另写阈值：

| 函数 | 用途 |
|------|------|
| `judge(dm, mz, dev, params)` | 单次判定：返回 `dm / mz / fhsw_da / k / need_da / valley_pct / overlap` |
| `check_list(values, dev, params)` | 取一组 m/z 的最近邻对判定；不足 2 个有效值返回 `None` |
| `resolve_k(params)` | 解析判据系数（谷值预设或自定义 k） |
| `resolve_fhsw_da(dev, mz, params)` | 解析实际峰宽（用户输入优先于设备标称） |
| `valley_to_k(v)` / `k_to_valley(k)` | 谷值 ↔ 判据系数，互为严格反函数 |

---

## 6. 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功（`--check` 检测完成，或服务正常终止） |
| 1 | 参数错误（argparse 抛出） |
| 2 | 参数解析错误（argparse 标准退出码） |

服务模式下进程常驻，通过 `server.pid` 与信号终止。
