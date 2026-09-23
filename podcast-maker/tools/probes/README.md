# tools/probes/

诊断、取证、实测类的脚本。**可复跑，所以入库**；它们跑出来的东西不在这里。

## 规矩

| 什么 | 放哪 | 入库 |
|---|---|---|
| 探针 / 验收 / 取证 **脚本** | 本目录 | 是 |
| 它们的**产物**（音频、json、报告页、截图） | `_smoke/` | 否（`.gitignore` 整目录排除） |

**为什么分开**：本目录曾经就是 `_smoke/`。而 `_smoke/` 被 gitignore 整目录排除，
于是源码、测试、文档去引用一个不入库的目录——对外 clone 必然报错。现在脚本归这里、
产物归 `_smoke/`，两边互不牵扯：**`_smoke/` 可以整目录删掉而不断任何链接**。

## 路径锚点约定

```python
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#     probes ↑        tools ↑       项目根 ↑

HERE = os.path.join(ROOT, "_smoke")          # 产物锚点，产物路径一律从这里接
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 要 import 同目录模块时
```

产物一律落 `_smoke/`，所以本目录在任何机器上检出即可跑，不需要先建目录。

## 大致分几类

- **`bgm_gen.py`** — 内置 BGM 素材合成器。产物已入库在 `podcast_maker/resources/bgm/`，
  产品运行时不依赖它，只在「加档位 / 重造素材」时才用。响度归一与
  `tools/bgm_loudness.py` 共用同一套实现。
- **`probe_*.py`** — 单点实测：TTS 音色 / 情绪 / 温度 / 确定性、LLM 行为、断行、门禁……
- **`make_*_page.py`、`build_*_report.py`** — 把实测数据与音频拼成单文件试听页
  （音频 base64 内嵌，零依赖）。
- **`check_buttons.js`** — 浏览器外的渲染体检：把卡片渲染函数真跑一遍，逐条校验拼串
  出来的事件属性语法与下拉过滤。见 `README.md`「端到端为什么要走 HTTP」一节。
- **`e2e_*.py`** — 端到端验收（起真服务、跑真权重）。
