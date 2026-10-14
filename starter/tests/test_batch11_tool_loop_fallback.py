"""批次 11 回归测试：模型不收敛时，手里有料就先交付（缺陷 27）。

病症：模型连着调工具、始终不给最终回答，7 轮之后撞上
  live.py:145  raise LLMError("tool_loop", ...)
而 :145 在 for 循环**外面**，:99 那个唯一交付口在循环**里面**——
所以只要这 7 轮里取到过数据，evidence 里其实是有料的，却因为 raise
被整个丢掉：用户拿到的是"模型服务这次没有正常返回（工具调用没有收敛）"。

真正没料的情况（工具全报错、也没检索过）才该 raise——那种情况下
拒绝回答才是诚实的。

修复前预期：test_loop_* 三条为红（全部退化成 refusal）。
守卫（真没料仍拒绝、一轮答完不受影响）修复前就该是绿的。
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

#: 能答出数的数据题：走通之后 answer_type 应该是 data，不是 refusal。
DATA_QUESTION = "2026年7月的净营业额是多少"


def _reply(tool: str, args: dict, index: int) -> LLMReply:
    call = {
        "id": "call_%d" % index,
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    message = {"role": "assistant", "content": "", "tool_calls": [call]}
    return LLMReply(
        message=message, finish_reason="tool_calls", content="", tool_calls=[call], elapsed=0.0
    )


def _loop_client(tool: str, args: dict):
    """永远只调同一个工具、从不下结论的假模型。"""

    class _LoopClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def chat_with_retry(self, messages, tools=None, budget=None, on_call=None):
            self.rounds = getattr(self, "rounds", 0) + 1
            return _reply(tool, args, self.rounds)

    return _LoopClient


def _answer_after_tools_client(answer_text: str = "根据工具返回的结果，这个区间的净营业额已列在下方数据里。"):
    """第一轮取数、第二轮给结论——正常收敛的假模型。

    注意不能用"一轮直接给答案"来当守卫：那种情况 evidence 是空的，
    代码按设计就该给 refusal（没有证据不贴 data 标签），那是另一个正确行为。
    """

    class _Client:
        def __init__(self, *a, **kw) -> None:
            self.rounds = 0

        def chat_with_retry(self, messages, tools=None, budget=None, on_call=None):
            self.rounds += 1
            if self.rounds == 1:
                return _reply("query_metrics", {"start": "2026-07-01", "end": "2026-07-31"}, 1)
            message = {"role": "assistant", "content": answer_text}
            return LLMReply(
                message=message, finish_reason="stop", content=answer_text, tool_calls=[], elapsed=0.0
            )

    return _Client


@pytest.fixture(scope="module")
def make_service(tmp_path_factory):
    """live 模式的服务（llm 三件套非空），但假模型不联网。"""
    tmp = tmp_path_factory.mktemp("batch11")

    def _build() -> Service:
        settings = Settings(
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
        return Service(settings)

    return _build


# -- 主断言：有料就要交付 -------------------------------------------------------


def test_loop_with_evidence_falls_back(make_service, monkeypatch):
    """取到过数据却不收敛——必须用已有证据作答，不能退化成 refusal。"""
    monkeypatch.setattr(
        service_module,
        "LLMClient",
        _loop_client("query_metrics", {"start": "2026-07-01", "end": "2026-07-31"}),
    )
    got = make_service().chat(None, DATA_QUESTION)

    assert got["answer_type"] != "refusal", "手里有数据却拒绝了：%s" % got["answer"]
    assert "没有正常返回" not in got["answer"]


def test_loop_with_retrieval_falls_back(make_service, monkeypatch):
    """只检索过文档（没取过数）也一样：有 retrieved 就该交付。"""
    monkeypatch.setattr(
        service_module, "LLMClient", _loop_client("search_kb", {"query": "退款政策"})
    )
    got = make_service().chat(None, "退款政策的时限是多少")

    assert got["answer_type"] != "refusal", "检索到了文档却拒绝了：%s" % got["answer"]


def test_fallback_is_recorded_in_trace(make_service, monkeypatch):
    """降级这件事必须在 trace 里留痕，否则现场看到的是"凭空冒出来的答案"。"""
    monkeypatch.setattr(
        service_module,
        "LLMClient",
        _loop_client("query_metrics", {"start": "2026-07-01", "end": "2026-07-31"}),
    )
    svc = make_service()
    got = svc.chat(None, DATA_QUESTION)
    detail = svc.get_trace(got["trace_id"])

    steps = [step["step"] for step in detail["steps"]]
    assert "tool_loop_fallback" in steps, "降级没有留痕，事后无法解释答案从哪来"
    assert "answer_live_failed" not in steps, "还在走 raise 那条路"


# -- 守卫：真没料仍然拒绝，正常路径不受影响 --------------------------------------


def test_loop_with_nothing_still_refuses(make_service, monkeypatch):
    """守卫：工具全报错、也没检索过——这种情况必须继续拒绝，不许硬答。"""
    monkeypatch.setattr(
        service_module, "LLMClient", _loop_client("run_sql", {"sql": "SELECT 1 FROM 不存在的表"})
    )
    got = make_service().chat(None, DATA_QUESTION)

    assert got["answer_type"] == "refusal"
    assert "没有正常返回" in got["answer"]


def test_normal_convergence_unaffected(make_service, monkeypatch):
    """守卫：取数→作答的正常收敛路径，不能被这次改动碰到（也不该记降级痕）。"""
    monkeypatch.setattr(service_module, "LLMClient", _answer_after_tools_client())
    svc = make_service()
    got = svc.chat(None, DATA_QUESTION)
    steps = [step["step"] for step in svc.get_trace(got["trace_id"])["steps"]]

    assert got["answer_type"] != "refusal"
    assert "tool_loop_fallback" not in steps, "正常收敛却被记了降级"
