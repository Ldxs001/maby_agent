# 变更日志

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