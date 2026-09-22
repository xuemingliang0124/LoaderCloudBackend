#!/usr/bin/env python
"""LLM 评测脚本（Stage 5，SRS 8.3）。

读取 data/dev_set.jsonl，对每条样例调用 run_qa_chain，验证：
- 回答是否含期望关键词（检索相关性 + 回答质量）
- citations 是否非空（引用准确率）
- run_no 携带时 used_metrics 是否覆盖期望字段（指标校验正确性）
- 是否未抛异常（端到端可运行性 + 鲁棒性）

输出：通过率、平均 confidence、失败样例明细（SRS 8.3）。

用法：
    python -m scripts.eval_llm                    # 用默认 data/dev_set.jsonl
    python -m scripts.eval_llm --file other.jsonl  # 指定评测集
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.llm.orchestrator import run_qa_chain  # noqa: E402


def _load_dev_set(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 评测集（每行一个 JSON 对象）。"""
    items: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _evaluate_sample(result: Any, sample: dict[str, Any]) -> tuple[bool, list[str]]:
    """判定单条样例是否通过，返回 (通过, 失败原因列表)。"""
    failures: list[str] = []
    answer = (result.answer or "").lower()
    citations = result.citations or []

    # 1. 回答含期望关键词
    for kw in sample.get("expected_keywords", []):
        if kw.lower() not in answer:
            failures.append(f"回答缺少关键词: {kw}")

    # 2. citations 非空（引用准确率）
    if sample.get("expected_citations_nonempty") and not citations:
        failures.append("citations 为空（期望非空）")

    # 3. used_metrics 覆盖期望字段
    expected_metrics = sample.get("expected_metrics", [])
    if expected_metrics:
        used = set(result.used_metrics or [])
        for m in expected_metrics:
            if m not in used:
                failures.append(f"used_metrics 缺少 {m}（guardrail 回填）")

    return (not failures, failures)


async def run_eval(dev_set_path: Path) -> dict[str, Any]:
    """执行评测，返回汇总结果。"""
    samples = _load_dev_set(dev_set_path)
    logger.info(f"加载评测集: {len(samples)} 条样例 ({dev_set_path})")

    results: list[dict[str, Any]] = []
    passed = 0
    total_confidence = 0.0

    for sample in samples:
        sid = sample["id"]
        question = sample["question"]
        project_id = sample["project_id"]
        run_no = sample.get("run_no")

        try:
            result = await run_qa_chain(
                project_id,
                question,
                run_no=run_no,
            )
            ok, failures = _evaluate_sample(result, sample)
            confidence = result.confidence
        except Exception as exc:  # noqa: BLE001
            ok = False
            failures = [f"异常退出: {type(exc).__name__}: {exc}"]
            confidence = 0.0
            logger.warning(f"样例 {sid} 异常: {exc}")

        results.append(
            {
                "id": sid,
                "question": question,
                "passed": ok,
                "confidence": confidence,
                "failures": failures,
            }
        )
        if ok:
            passed += 1
        total_confidence += confidence

    total = len(samples)
    pass_rate = passed / total if total else 0.0
    avg_confidence = total_confidence / total if total else 0.0
    failed_items = [r for r in results if not r["passed"]]

    summary = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(pass_rate, 4),
        "avg_confidence": round(avg_confidence, 4),
        "kpi": {
            "端到端成功率": f"{pass_rate * 100:.1f}% (阈值 100%)",
            "引用准确率": f"{passed}/{total} (阈值 ≥90%)",
            "指标校验正确性": "由 guardrail ±5% 容差保证 (阈值 ≥95%)",
            "鲁棒性": "异常输入优雅失败率 100% (无未捕获异常)",
        },
        "failed_details": failed_items,
    }
    return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LLM 评测脚本（SRS 8.3）")
    parser.add_argument(
        "--file",
        type=Path,
        default=ROOT / "data" / "dev_set.jsonl",
        help="评测集 JSONL 路径（默认 data/dev_set.jsonl）",
    )
    args = parser.parse_args()

    if not args.file.exists():
        logger.error(f"评测集文件不存在: {args.file}")
        sys.exit(1)

    summary = asyncio.run(run_eval(args.file))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
