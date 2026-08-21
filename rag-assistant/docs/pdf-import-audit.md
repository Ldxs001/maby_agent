# PDF 导入链路审查报告

> 审查日期：2026-08-21（初版），2026-08-21 更新（pypdfium2 改造后）
> 审查工具：CodeArts + GLM-5.2
> 审查范围：PDF 从用户上传到存入 ChromaDB+HNSW 的完整链路
> 当前状态：pypdf → pypdfium2 迁移已完成，形似字/词间距丢失问题已从根源消除

---

## 1. PDF 导入完整调用链

### 1.1 流程图（pypdfium2 架构）

```
[用户入口]
  ├─ rag_wrapper.py:74  RAGWrapper.import_file()
  ├─ rag_skill.py:49    run_import()
  └─ rag_standalone.py:387  CLI --import-file
        │
        ▼
[入口函数] rag_core.py:562  import_documents_to_kb(file_path, kb_name, embeddings, splitter_config)
        │
        ├─ :582  ext = os.path.splitext(file_path)[1].lower()
        ├─ :583  if ext == ".pdf":  ← PDF 路由（只看扩展名，不读 enable_pdf 配置）
        │       │
        │       ├─ :584  import pypdfium2 as pdfium     ← pypdfium2 硬编码（Google PDFium 引擎）
        │       │
        │       ├─ :586-590  步骤1：二进制读取判断 PDF 类型
        │       │       ├─ has_font = b'/Font' in raw
        │       │       └─ has_image = b'/Image' in raw or b'/XObject' in raw
        │       │
        │       ├─ :594-614  OCR 预判
        │       │       ├─ :594  if not has_font: → 直接 OCR（无文本层）
        │       │       └─ :597-614  elif has_image: 统计无文本页占比 > 50% → OCR
        │       │
        │       ├─ :616-630  步骤2：pypdfium2 提取全部页文本
        │       │       └─ page.get_textpage().get_text_range()  ← 纯文本，无图片无表格
        │       │
        │       ├─ :632-655  步骤3：信号 2+4 逐页质量检测
        │       │       ├─ 信号2：英文词间距丢失（alpha>0.5 且 max_run>30）
        │       │       └─ 信号4：中文常用字覆盖率 < 50%
        │       │       └─ 乱码页占比 > 10% → 整篇 OCR
        │       │
        │       └─ :657-675  步骤4：OCR 回退（仅当 need_ocr=True）
        │               ├─ pdf2image + easyocr
        │               └─ metadata={"source":..., "ocr": True}  ← 只有 OCR 路径写 ocr 字段
        │
        ├─ :686-705  读取 splitting 配置，组装 pipeline_kwargs
        │
        ├─ :715-731  if preprocess.enabled:  ← rag_config.json 无此配置，走 else
        │       └─ 合并所有页 → apply_markdown_preprocess → split_pipeline
        │
        ├─ :732-746  else: 逐页切分（实际走这条）
        │       ├─ :740  page_chunks = split_pipeline(page_text, **pipeline_kwargs)
        │       ├─ :742  c.metadata = dict(page_doc.metadata)  ← 覆盖 chunk 自身 metadata
        │       └─ :746  mark_header_chunks(chunks)
        │
        └─ :748  ok, msg = add_documents_to_kb(kb_name, chunks, embeddings)
                │
                ▼
[入库] knowledge_base_manager.py  add_documents_to_kb()
        ├─ SM3 哈希去重
        ├─ vectorstore.add_documents(documents, ids=doc_ids)
        │       │
        │       ▼
[存储] chroma_adapter.py  Chroma.add_documents()
        ├─ embeddings = self._embedding_function.embed_documents(texts)  ← 算向量
        ├─ self._chroma_coll.add(ids, documents, embeddings, metadatas)   ← 写 SQLite（metadata）
        └─ self._hnsw.add_items(embeddings, doc_ids)                      ← 写 hnswlib（向量）
```

### 1.2 切片内部流程（text_splitter.py split_pipeline）

```
split_pipeline(text, guards=["code","table","math"], primary="recursive", ...)
  ├─ guard_stack = GuardStack(guards)
  ├─ protected_text = guard_stack.apply(text)     ← 守卫 protect（替换为占位符）
  ├─ chunks = plugin.execute(protected_text)      ← 主切分（recursive, chunk_size=500）
  ├─ chunks = guard_stack.restore_chunks(chunks)  ← 守卫 restore（还原占位符）
  └─ 后处理子切（secondary_strategy="semantic"，仅对超长 chunk）
```

---

## 2. PDF 解析层现状

### 2.1 pypdfium2 调用位置

| 位置 | 用途 | 说明 |
|------|------|------|
| `rag_core.py:584` | **主导入链路** | `pdfium.PdfDocument` + `page.get_textpage().get_text_range()`，只提取纯文本 |
| `rag_skill.py:78` | 自动分类前的内容预读 | 二进制判断 + pypdfium2 提取，用于 auto_classify 路由 |
| `agent.py:1045` | 智能体预览 | 只读前 4 页且 `[:500]` 截断，非导入链路 |

**说明**：三处调用全部只用 `get_text_range()`，pypdfium2 的文本提取只返回纯字符串，**不返回图片、不返回表格结构、不返回布局信息**。这是设计意图——PDF 只留文本，图片/表格 `|` 都是语义噪音。

### 2.2 图片处理

**设计上不提取**。PDF 只保留文本是故意设计，图片不入库。全项目无任何代码把 PDF 图片提取为文件或生成 `![](path)` 语法。

### 2.3 表格处理缺口

**完全缺失**。全项目 grep 结果：
- `pdfplumber`：**0 处 import**，仅在 config/UI 中作为字符串提及
- `camelot`：**0 处**
- `tabula`：**0 处**

pypdfium2 的 `get_text_range()` 对表格只能按坐标顺序吐出空格对齐的纯文本，表格结构（行列对应关系）完全丢失。

### 2.4 PDF 解析返回数据结构

`rag_core.py:626-629`：返回 `List[Document]`，每个 Document 的 `page_content` 是**纯字符串**（单页文本），`metadata` 是 `{"source": filename, "page": i+1}`。**无任何结构化字段**（无 images、无 tables、无 layout）。

---

## 3. PDF 类型判断与质量检测（新增）

### 3.1 二进制类型判断（步骤 1，`rag_core.py:586-614`）

```python
with open(file_path, 'rb') as f:
    raw = f.read()
has_font = b'/Font' in raw
has_image = b'/Image' in raw or b'/XObject' in raw

if not has_font:
    # 无文本层 → 直接 OCR
    need_ocr = True
elif has_image:
    # 混合版：统计无文本页占比
    # 无文本页占比 > 50% → OCR
    need_ocr = True
```

**改进点**：旧版只靠 `total_chars < 50` 判断，扫描件 pypdf 常提取出 100-2000 字符乱码绕过。新版先用二进制判断 `/Font` 是否存在，无字体直接 OCR。

### 3.2 信号检测（步骤 3，`rag_core.py:632-655`）

提取后、切分前，逐页检测两种乱码信号：

| 信号 | 检测条件 | 目标 |
|------|----------|------|
| **信号 2** | `alpha > 0.5 且 max_run > 30`（英文词间距丢失） | 检测英文学术论文词间距丢失（如 `A methodfordeterminingthedermaltoxicity`） |
| **信号 4** | `len(cjk_chars) > 10 且 common_hit < 0.5`（中文常用字覆盖率） | 检测中文乱码（常用字覆盖率低） |

- 乱码页占比 > 10% → 整篇走 OCR
- **不剔除 chunk**：乱码 chunk 不剔除（可能有有效信息），让检索自然降权

### 3.3 已排除的检测方案

| 方案 | 排除原因 |
|------|----------|
| bigram 命中率 | 对形似字无效（形似字 chunk 的 bigram 命中率与正常 chunk 一样高） |
| 词典检测（nltk） | nltk 语料包下载失败 |
| 形似字词典 | 需维护，且 pypdfium2 已从根源消除形似字问题 |

---

## 4. OCR 回退逻辑现状

### 4.1 触发条件（`rag_core.py:594-655`，三层判断）

```
层1（二进制）：无 /Font → 直接 OCR
层2（混合版）：有 /Image 且无文本页占比 > 50% → 直接 OCR
层3（质量检测）：信号 2+4 乱码页占比 > 10% → 整篇 OCR
```

### 4.2 元数据写入逻辑

| 路径 | 代码位置 | metadata |
|------|----------|----------|
| pypdfium2 正常提取 | `rag_core.py:628` | `{"source": filename, "page": i+1}` — **无 ocr 字段**（设计意图） |
| OCR 回退 | `rag_core.py:672` | `{"source": filename, "ocr": True}` — **ocr=True** |

**OCR 标记设计**：正常文字版 PDF 不写 ocr 字段，只有 OCR 回退路径才写 `ocr: True`。

---

## 5. 守卫栈现状

### 5.1 内置守卫

| 守卫 | 正则表达式 | 说明 |
|------|-----------|------|
| mermaid | ` ```mermaid\s*\n[\s\S]*?\n``` ` | 保护 mermaid 流程图 |
| code | ` ```\w*\n[\s\S]*?\n``` ` | 保护围栏代码块 |
| math | ` \$\$[\s\S]*?\$\$ ` | 保护 LaTeX 公式 |
| **table** | ` (?:\|.*\|(?:\s*$)\n?){2,} ` | 只匹配 Markdown 表格 `|...|`，至少 2 行连续 |
| html | ` <(div\|table\|pre\|...)>[\s\S]*?</\1> ` | 保护 HTML 块级标签 |
| **image** | **不存在** | 无任何图片守卫 |

### 5.2 配置实际启用的守卫

`rag_config.json`：`"guards": ["code", "table", "math"]`
- 守卫栈（code/table/math）是为 Markdown 设计的，**PDF 不走守卫保护**（PDF 输出纯文本，不含 Markdown 语法）

### 5.3 table 守卫的局限性

table 守卫正则只匹配**已经是 Markdown 语法的表格**（`| col1 | col2 |`）。但 pypdfium2 的 `get_text_range()` 输出的是**空格对齐的纯文本**，不是 Markdown 表格语法。因此 table 守卫对 PDF 输出**完全无效**。

---

## 6. 配置项现状：活跃 vs 死配置

### 6.1 PDF 相关配置项

| 配置项 | 配置值 | 代码是否读取 | 状态 |
|--------|--------|-------------|------|
| `input_sources.enable_pdf` | `true` | **否** | **死配置**。`rag_core.py:583` 只看扩展名 `.pdf`，不读此开关 |
| `input_sources.enable_ocr` | `true` | **否** | **死配置**。OCR 触发只看类型判断/质量检测，不读此开关 |
| `input_sources.enable_html2md` | `true` | **否** | **死配置**。无任何 HTML→MD 转换代码 |
| `input_sources.pdf_backend` | `"pypdfium2"` | **否** | **死配置**。`rag_core.py:584` 无条件 `import pypdfium2`，不读此字段 |

### 6.2 切片相关配置项（全部活跃）

| 配置项 | 状态 |
|--------|------|
| `splitting.strategy` | 活跃 |
| `splitting.chunk_size` | 活跃 |
| `splitting.chunk_overlap` | 活跃 |
| `splitting.separators` | 活跃 |
| `splitting.guards` | 活跃（但 table 守卫对 PDF 输出无效） |
| `splitting.secondary_strategy` | 活跃 |

---

## 7. 依赖包确认

| 包 | 安装方式 | 代码使用情况 |
|----|----------|-------------|
| **pypdfium2** | `pip install`（requirements.txt） | **在用**（`rag_core.py:584`、`rag_skill.py:78`、`agent.py:1045`）。BSD-3-Clause / Apache-2.0 |
| easyocr | pip | **在用**（仅 OCR 回退路径） |
| pdf2image | pip | **在用**（仅 OCR 回退路径） |
| pdfplumber | 未安装 | 配置/UI 字符串提及，**0 处 import** |
| ~~pypdf~~ | 已移除 | 原 vendor/pypdf 已删除，改用 pypdfium2 |
| ~~PyMuPDF (fitz)~~ | 排除 | AGPL-3.0 许可证，与 PyPI 发布（Apache 2.0）不兼容 |

---

## 8. 问题清单

### 已修复 ✅

| # | 原问题 | 修复方式 |
|---|--------|----------|
| — | **形似字**（`Iethods`→`Methods`、`sarnple`→`sample`） | pypdf → pypdfium2（Google PDFium 引擎），从根源消除 |
| — | **词间距丢失**（`A methodfordeterminingthedermaltoxicity`） | 信号 2 检测 + pypdfium2 提取质量更好 |
| — | **OCR 触发条件漏判乱码 PDF** | 新增二进制类型判断 + 信号 2+4 质量检测，三层判断 |
| — | **vendor/pypdf 占空间** | 已删除，改 pip 安装 pypdfium2 |

### 仍存在 ⚠️

| # | 问题 | 严重程度 | 根因 | 影响 |
|---|------|----------|------|------|
| 1 | **表格结构 100% 破坏** | P1 | pypdfium2 只提取纯文本，无 pdfplumber/camelot/tabula；table 守卫对 PDF 输出无效 | 表格经 pypdfium2 提取后变空格对齐文本，行列对应关系丢失 |
| 2 | **4 个 PDF 配置项是死配置** | P2 | `enable_pdf`/`enable_ocr`/`enable_html2md`/`pdf_backend` 代码均未读取 | UI 切换无效果，造成"已启用"错觉 |
| 3 | **pdfplumber 配置/UI 提及但 0 实现** | P2 | 多处字符串提及，无 `import pdfplumber` | UI 显示"支持 pdfplumber"但实际不可用 |
| 4 | **chunk metadata 被覆盖** | P3 | `rag_core.py:742` `c.metadata = dict(page_doc.metadata)` 覆盖 chunk 自身 metadata | 若守卫还原产生 metadata（如 h1/h2/h3）会被清掉；当前因 guards 不含产生 metadata 的逻辑而未爆发 |

### 设计意图（非问题）

- **图片不提取**：PDF 只留文本是故意设计，图片/表格 `|` 都是语义噪音
- **守卫栈对 PDF 无效**：守卫栈为 Markdown 设计，PDF 不走守卫保护
- **正常路径不写 ocr 字段**：只有 OCR 回退路径才写 `ocr: True`

---

## 9. DB 实际数据验证（旧数据，pypdf 时期）

> **注意**：以下数据是用旧 pypdf 链路生成的 chunk 统计。pypdfium2 改造后需要重新导入才能反映新链路质量。

### 9.1 chunk 统计（4 个 KB，共 7552 chunk）

| KB | 总chunk | 含`\|` | 含图片`![` | OCR标记 | 空格对齐伪表格 |
|---|---|---|---|---|---|
| 理化检测 | 751 | 32 | 0 | 0 | 51 |
| 检测技术 | 3633 | 340 | 0 | 0 | 0 |
| 生物医疗 | 1698 | 7 | 0 | 0 | 36 |
| 设备条件 | 1470 | 4 | 0 | 0 | 2 |

### 9.2 提取质量样本（pypdf 时期的问题，pypdfium2 应已修复）

- **生物医疗**：英文学术论文词间距丢失（`A methodfordeterminingthedermaltoxicity`），pypdf 对扫描版老论文提取效果极差
- **检测技术**：英文教材出现 `Iethods`（应为 Methods）、`sarnple`（应为 sample）等形似字，OCR 标记为 0
- **设备条件**：中文仪器参数格式 `键 值`，正常

### 9.3 pypdfium2 验证

- `9704142v1.pdf` 页 4/19：pypdf 提取 `fro m`（形似字），pypdfium2 提取 `from`（正确）✅
- 形似字问题已从根源消除

---

## 10. 后续建议

1. **重新导入现有 KB**：现有 7552 chunk 是用旧 pypdf 生成的，需用 pypdfium2 重新导入才能修复存量数据
2. **表格提取**（如需要）：安装 pdfplumber，在 pypdfium2 提取后追加表格提取 → 转 Markdown `|...|` 语法
3. **死配置清理**：让代码读取 `enable_pdf`/`pdf_backend`/`enable_ocr`，或从 UI 移除这些开关
4. **metadata 合并**：`rag_core.py:742` 改为 `{**dict(page_doc.metadata), **c.metadata}` 避免 chunk 自身 metadata 被覆盖
