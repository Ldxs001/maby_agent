# 变更日志

## 0.1.2 — 仓库卫生修复

- **现象**：`__pycache__/` 字节码被纳入版本控制，13 个 `.pyc` 内嵌本机绝对路径；项目根缺 `.gitignore`
- **根因**：项目无 `.gitignore`，同步流程按整目录复制，源码侧 `__pycache__/` 被原样搬入仓库并被 `git add` 全量收录
- **修复**：新增 `.gitignore`；从版本控制移除 `__pycache__/` 下的 13 个 `.pyc`
- **验证**：`git ls-files` 无 `.pyc` / `__pycache__` 命中

## 0.1.1 — 2026-09-10

### 修复（Apache-2.0 合规）
- **现象**：项目无 `NOTICE` 文件，归属声明无载体
- **根因**：`NOTICE` 从未建立；`LICENSE`（Apache-2.0 全文）虽完整但缺配套归属声明
- **修复**：新增 `NOTICE`（项目名 + 版权署名 + 许可证指引），与 lc-ms-group-advisor 口径一致
- **验证**：版本四处（`silprespec_orchestrator/__init__.py` / README.md / llms.txt / PROTOCOL.md）同步为 0.1.1

## 0.1.0 — 2026-08-31

### 新增
- 建项目骨架 silprespec-orchestrator
- 14 种穷举组合声明（combo_registry.py）
- 标准化工具接口 ToolSpec + 三智能体注册（tool_registry.py）
- 进度地图 + 输入分类（progress_map.py）
- 编排器主控：分类→选编排模式→分解子任务→执行→汇总（orchestrator.py）
- Mapper：选组合+设参，含 output_limit（mapper.py）
- Composer：PY 确定性组合，调 exec_recipe（composer.py）
- Executor：LLM 填空+调智能体 API（executor.py）
- Adapter：步骤间适配，不能直通则 loop 回 Mapper（adapter.py）
- 原子库复用：atoms.py + pipeline_model.py + llm_client.py
- Web UI（端口 8789）
- setup.bat 一键启动
- PROTOCOL.md 协议文档