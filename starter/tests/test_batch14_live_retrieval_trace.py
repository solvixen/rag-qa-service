"""批次 14 回归测试：live 路径的检索必须留痕（评审点名第 3 条）。

评审反馈原文：
  「调试面板只把每一步的原始 JSON 折叠展示，读起来费力；接上真实模型后，
    模型自己调用的检索只记工具名和参数，看不到检索片段的分数和被过滤的原因。」

（live.py 的 trace.step("tool", {"tool": name, "params": params}) 只留了名和参数。）

修复：search_kb 的调用把 SearchResult.as_trace() 一起写进 trace（片段分数、
被过滤的原因、词扩展、覆盖率），同时保证这份诊断**不喂给模型**——
模型上下文里只能看到干净的 results。

修复前预期：三条 test_trace_* 为红；模型侧与契约侧的两条守卫为绿。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kbqa import service as service_module  # noqa: E402
from kbqa.config import Settings  # noqa: E402
from kbqa.llm import LLMReply  # noqa: E402
from kbqa.service import Service  # noqa: E402

WORKSPACE = ROOT.parent
QUESTION = "退款政策的时限是多少"


def make_settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=WORKSPACE / "data",
        kb_dir=WORKSPACE / "knowledge_base",
        var_dir=tmp / "var",
        today=date(2026, 9, 1),
        llm_base_url="http://fake.local/v1",
        llm_api_key="test-key",
        llm_model="fake-model",
        llm_timeout=120.0,
        chat_budget=150.0,
    )


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    return Service(make_settings(tmp_path_factory.mktemp("batch14")))


def _tool_reply(tool: str, args: dict) -> LLMReply:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    message = {"role": "assistant", "content": "", "tool_calls": [call]}
    return LLMReply(
        message=message, finish_reason="tool_calls", content="", tool_calls=[call], elapsed=0.0
    )


def _answer_reply(text: str) -> LLMReply:
    message = {"role": "assistant", "content": text}
    return LLMReply(
        message=message, finish_reason="stop", content=text, tool_calls=[], elapsed=0.0
    )


def _run_live_with_search(monkeypatch, tmp_path: Path, captured: list) -> dict:
    """跑一次 live 问答：第一轮让"模型"调 search_kb，第二轮给结论。

    `captured` 会收到第二轮发给模型的完整 messages，用来检查诊断信息有没有漏进去。
    """

    class _Client:
        def __init__(self, *a, **kw) -> None:
            self.rounds = 0

        def chat_with_retry(self, messages, tools=None, budget=None, on_call=None):
            self.rounds += 1
            if self.rounds == 1:
                return _tool_reply("search_kb", {"query": "退款 时限"})
            captured.extend(messages)
            return _answer_reply("退款时限以文档为准。")

    monkeypatch.setattr(service_module, "LLMClient", _Client)
    svc = Service(make_settings(tmp_path))
    return svc, svc.chat(None, QUESTION)


# -- 主断言：检索诊断必须进 trace -----------------------------------------------


def test_search_tool_carries_diagnostics(svc):
    """工具返回里要带上检索诊断，供 live 侧写 trace。"""
    got = svc.run_tool("search_kb", {"query": "退款 时限", "top_k": 3})
    assert "_diagnostics" in got, "search_kb 没有把检索诊断带出来"
    diag = got["_diagnostics"]
    assert "filtered" in diag, "看不到被过滤的原因"
    assert "hits" in diag and diag["hits"], "看不到片段分数"
    for hit in diag["hits"]:
        assert "score" in hit and "chunk_id" in hit


def test_trace_records_retrieval_scores(monkeypatch, tmp_path):
    """live 的 trace 里，search_kb 那一步要能看到片段分数。"""
    captured: list = []
    svc, got = _run_live_with_search(monkeypatch, tmp_path, captured)

    detail = svc.get_trace(got["trace_id"]) or {}
    steps = [s for s in detail.get("steps", []) if s["step"] == "tool"]
    assert steps, "连 tool 这一步都没有"

    retrieval = steps[0]["detail"].get("retrieval")
    assert retrieval, "search_kb 那一步没记检索诊断——评审要的正是这个"
    assert retrieval["hits"][0]["score"] is not None
    assert "filtered" in retrieval
    assert "coverage" in retrieval


def test_trace_records_filtered_reasons(monkeypatch, tmp_path):
    """有文档被元数据过滤掉时，原因要留在 trace 里（而不是静默消失）。"""
    captured: list = []
    svc, got = _run_live_with_search(monkeypatch, tmp_path, captured)
    detail = svc.get_trace(got["trace_id"]) or {}
    step = next(s for s in detail["steps"] if s["step"] == "tool")
    # filtered 即使是空列表也必须存在这个键——空和"没记"是两回事
    assert isinstance(step["detail"]["retrieval"]["filtered"], list)


# -- 守卫：诊断不能漏给模型，契约不能变 ------------------------------------------


def test_model_never_sees_diagnostics(monkeypatch, tmp_path):
    """守卫：检索诊断只进 trace，绝不能进模型上下文（白占 token 且会误导模型）。"""
    captured: list = []
    _run_live_with_search(monkeypatch, tmp_path, captured)

    assert captured, "没抓到发给模型的 messages"
    joined = json.dumps(captured, ensure_ascii=False)
    assert "_diagnostics" not in joined, "诊断信息漏给模型了"


def test_retrieve_shape_unchanged(svc):
    """守卫：/api/retrieve 与 search_kb 给模型的 results 结构保持契约 §4 不变。"""
    got = svc.run_tool("search_kb", {"query": "退款政策", "top_k": 3})
    assert "results" in got
    for hit in got["results"]:
        assert set(hit) == {"doc_id", "chunk_id", "score", "text"}
