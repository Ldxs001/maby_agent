"""前置规范效果实验台 — 执行引擎

对用户选的每种前置规范方式，真实调用 LLM 填空，记录：
  - 填入了什么（实际内容）
  - 重试次数、是否撑满 max_retry
  - 各方式专属观测（命中分布/编造项/纠偏前后...）

8 种方式都是前置规范（生成通道/填空出口），后置验证不在本系统。
并行 N 次观测重现性。

执行逻辑由原子配方驱动（atoms.exec_recipe），本文件只做并行编排。
"""
from __future__ import annotations
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .pipeline_model import (
    Experiment, WayConfig, WayResult, RunResult, calc_reproducibility,
)
from .llm_client import LLMClient, LLMClientError
from .atoms import exec_recipe


class ExperimentRunner:
    """前置规范效果实验台执行器"""

    def __init__(self, llm: LLMClient, verbose: bool = False):
        if llm is None:
            raise ValueError("实验台必须接入 LLM（要真实填空观测填入内容）")
        self.llm = llm
        self.verbose = verbose

    # ------------------------------------------------------------------
    # 并行入口
    # ------------------------------------------------------------------
    def run(self, experiment: Experiment, user_input: str,
            parallel: Optional[int] = None) -> dict:
        if isinstance(experiment, dict):
            experiment = Experiment.from_dict(experiment)
        n = parallel or experiment.parallel or 5
        if n < 1:
            n = 1
        if self.verbose:
            print(f"[实验台] 启动 {n} 并行，方式数={len([w for w in experiment.ways if w.enabled])}")

        results: list = []
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {pool.submit(self._run_one, experiment, user_input, rid): rid
                       for rid in range(1, n + 1)}
            for fut in as_completed(futures):
                rid = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = RunResult(run_id=rid).to_dict()
                    res["error"] = f"{e}\n{traceback.format_exc()}"
                results.append(res)
        results.sort(key=lambda r: r.get("run_id", 0))
        repro = calc_reproducibility([r for r in results if "way_results" in r])
        return {"runs": results, "reproducibility": repro}

    # ------------------------------------------------------------------
    # 单次执行：对每种启用方式真实填空（方式间串行，不传状态）
    # ------------------------------------------------------------------
    def _run_one(self, experiment: Experiment, user_input: str, run_id: int) -> dict:
        res = RunResult(run_id=run_id)
        for wc in experiment.ways:
            if not wc.enabled:
                continue
            wr = self._exec_way(wc, user_input, run_id)
            res.way_results.append(wr)
        return res.to_dict()

    def _exec_way(self, wc: WayConfig, user_input: str, run_id: int) -> WayResult:
        try:
            return exec_recipe(wc.way, wc, user_input, self._chat)
        except Exception as e:
            return WayResult(way=wc.way, error=f"{e}\n{traceback.format_exc()}")

    def _chat(self, prompt, max_tokens=None, temperature=0.5, system_prompt=None):
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})
        return self.llm.chat(msgs, max_tokens=max_tokens, temperature=temperature).strip()
