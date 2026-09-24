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
- **`subtitle_roll_check.py`** — 单行滚动档的**真帧复验**（libass 实渲，不是推算）：
  句首 / 滚动中 / 收尾三帧看图 + 量墨迹包围盒；另把同一句放进 4000px 宽画布量真实
  墨迹宽，与 `text_px_width` 的算式对账（算式不得更窄、偏差 ≤ 2%）。
- **`subtitle_recap_check.py`** — 拿真实项目的**回顾三条**现算（`review_rows`）＋真实估时
  （`duration_model`），逐句验算横滚滚不滚得完。终点与句末的差只可能是 ASS 时间戳
  （10ms 一格）的取整残差，所以判定用 `TAIL_TOLERANCE = 20ms`，拿 `end <= span` 卡会误报。
- **`make_*_page.py`、`build_*_report.py`** — 把实测数据与音频拼成单文件试听页
  （音频 base64 内嵌，零依赖）。
- **`check_buttons.js`** — 浏览器外的渲染体检：把卡片渲染函数真跑一遍，逐条校验拼串
  出来的事件属性语法与下拉过滤。见 `README.md`「端到端为什么要走 HTTP」一节。
- **`e2e_*.py`** — 端到端验收（起真服务、跑真权重）。
- **`verify_pypi_wheel.py`** — 发布前干跑构建：照打包模板在本地构建一次 wheel（**不上传**），
  列出内容，并核对「`entry_points` 声明的模块是否真在包内」——这条只能把 wheel 解开逐条看
  才会暴露（声明了入口却没打进包里，装完命令直接失败）。加 `--against <另一个.whl>` 按内容比对。
- **合成链的静态层与性能**（一组，问「出片为什么慢」时从这几个里挑）：
  - **`bake_static_check.py`** — 静态层烘焙的**对账**：烘焙图 vs 原滤镜链逐像素比，另带
    `--calibrate` 模式把字幕颜色的**有限范围口径**（`Y = 16 + 219·c/255`）与描边外扩逐组校准。
  - **`box_onset_verify.py`** — 「字幕框与第一句字幕同时出现」的**验收**：调的就是出片时那两个
    函数（`bake_static_layers` ＋ `video_engine.compose`），三份产物（现状 / 候选 / 不画框参照）
    比 PSNR 与帧数。判据：框出现前候选须等于参照、框出现后等于现状、帧数一分不差。
  - **`box_onset_probe.py`** — 同一件事的**选型**探针（四种切法比墙钟）：决定用「两张图按帧接」
    而不是「叠框那一小块 + 时间开关」（后者慢一倍多）。
  - **`render_scale_scan.py`** — 并行度扫描：串行 / 空字幕 / 各种 N 路 × 编码线程 ×
    滤镜图线程，逐条打印每段耗时与**核当量**。
  - **`render_stage_ceiling.py`** — 把全链拆成八级分别计时，看每一级的天花板。
  - **`parallel_render_check.py`** — 分段并行的**正确性**对账：两路都用 `crf 0` 无损编、
    逐帧比 MSE（必须全 0）＋ 各自对无损真值比 PSNR。
  - **`hv_parallel_check.py`** — 横竖两版并发出片 vs 串行。
  
  结论一句话：**这条链的墙是每帧搬运量（进程间共享的内存带宽），不是 CPU**——分段并行实测
  0.88x（净亏）、横竖并行 1.13x，所以 `WORKERS_MAX = 1`、默认一次渲到底。上面这几个脚本是
  那份结论的全部证据，换链再量一遍就能重判。
