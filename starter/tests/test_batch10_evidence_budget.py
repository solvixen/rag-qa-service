"""缺陷 26：evidence 数字总量无约束 —— 多次工具调用累加越评测上限。

live 下模型可能连调多个宽工具（H04: top_products 30 + unit_price_check 31 = 61，
评测上限 60），答案全对却死在 evidence_hygiene。修法：输出前按总量预算裁剪。
"""

import json

from kbqa.live import _NUMBER, trim_evidence


def _count(result) -> int:
    return len(_NUMBER.findall(json.dumps(result, ensure_ascii=False)))


def _wide_tool(count: int, tag: str) -> dict:
    """构造一条带 count 个数字的工具证据。"""
    rows = [{"%s_value" % tag: i} for i in range(count)]
    return {"tool": "demo", "params": {}, "result": {"rows": rows}}


def test_over_budget_gets_trimmed():
    """复刻 H04：30 + 31 = 61 个数字，裁剪后必须 <= 50。"""
    evidence = [_wide_tool(30, "a"), _wide_tool(31, "b")]
    kept = trim_evidence(evidence)
    total = sum(_count(item["result"]) for item in kept)
    assert total <= 50, "裁剪后仍超预算: %d" % total


def test_never_empty():
    """即使单条就超预算，也必须保底留一条（evidence_required 查非空）。"""
    evidence = [_wide_tool(80, "a")]
    kept = trim_evidence(evidence)
    assert len(kept) >= 1, "evidence 被裁空"


def test_under_budget_untouched():
    """预算内不动：条数与顺序原样保留。"""
    evidence = [_wide_tool(6, "a"), _wide_tool(9, "b")]
    kept = trim_evidence(evidence)
    assert kept == evidence
