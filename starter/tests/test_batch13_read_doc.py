"""批次 13 回归测试：新增 read_doc(doc_id) 工具，修 T02 类"召回了文档、召不回答案"。

病灶（live 混合题失分的主因之一）：
  问"三文鱼供应商赔了多少钱"，检索能召回 KB-022（英文商务邮件），但 top-k 里
  返回的是它的 Subject 行（KB-022#2）；含答案的 KB-022#9
  （"We will issue a credit note of CNY 8,600 against your July account."）
  因为整段不含任何中文查询词，BM25 打不上分。
  模型看到"像是对的那篇文档"却拿不到数字，只能反复换词检索，最后撞上 tool_loop。

修法：给模型一个 read_doc(doc_id) —— 已经知道文档编号时，直接读全文，
绕开分块排名。这是检索链路之外的一条确定性通道。

修复前预期：test_read_doc_* 全部为红（工具还不存在，run_tool 返回"没有这个工具"）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kbqa.config import Settings  # noqa: E402
from kbqa.service import Service  # noqa: E402
from kbqa.toolspec import TOOL_NAMES, TOOLS  # noqa: E402

WORKSPACE = ROOT.parent

#: T02 的标本：答案在这封英文邮件的第 9 段，整段不含中文查询词。
T02_DOC = "KB-022"
T02_QUERY = "三文鱼供应商赔偿"
T02_ANSWER_TEXT = "CNY 8,600"


def make_service(tmp_path: Path) -> Service:
    settings = Settings(
        data_dir=WORKSPACE / "data",
        kb_dir=WORKSPACE / "knowledge_base",
        var_dir=tmp_path / "var",
        today=date(2026, 9, 1),
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_timeout=120.0,
        chat_budget=150.0,
    )
    return Service(settings)


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    """只读用例，共用一个 Service 即可（建索引比较慢）。"""
    return make_service(tmp_path_factory.mktemp("batch13"))


# -- 工具注册 -------------------------------------------------------------------


def test_read_doc_is_registered():
    """工具必须真的暴露给模型，且有能讲清用途的描述。"""
    assert "read_doc" in TOOL_NAMES, "read_doc 没注册，模型看不到这个工具"
    spec = next(t["function"] for t in TOOLS if t["function"]["name"] == "read_doc")
    assert "doc_id" in spec["parameters"]["properties"]
    assert "doc_id" in spec["parameters"]["required"]
    assert len(spec["description"]) > 10, "描述太短，模型不知道该在什么时候用"


# -- 现状钉住：检索确实拿不到答案 ------------------------------------------------


def test_search_cannot_reach_the_answer_chunk(svc):
    """前提检查：search_kb 召回了 KB-022，但片段里没有答案数字。"""
    got = svc.run_tool("search_kb", {"query": T02_QUERY, "top_k": 5})
    hits = [h for h in got["results"] if h["doc_id"] == T02_DOC]
    assert hits, "KB-022 没被召回，这个测试的前提变了"
    assert not any(T02_ANSWER_TEXT in h["text"] for h in hits), (
        "答案片段居然已经被召回了，read_doc 的必要性需要重新评估"
    )


# -- read_doc 的核心能力 --------------------------------------------------------


def test_read_doc_returns_the_full_text(svc):
    """按编号读全文：必须能拿到答案所在的整段。"""
    got = svc.run_tool("read_doc", {"doc_id": T02_DOC})
    assert "error" not in got, got
    assert T02_ANSWER_TEXT in got["text"], "read_doc 没给出含答案的正文，T02 还是修不掉"
    assert got["doc_id"] == T02_DOC


def test_read_doc_text_is_longer_than_any_chunk(svc):
    """守卫：读的是整篇，不是某一段。"""
    got = svc.run_tool("read_doc", {"doc_id": T02_DOC})
    longest = max(len(c.text or "") for c in svc.index.chunks_of(T02_DOC))
    assert len(got["text"]) > longest


def test_read_doc_is_case_insensitive(svc):
    """模型偶尔写小写编号，不该因此失败。"""
    got = svc.run_tool("read_doc", {"doc_id": "kb-022"})
    assert "error" not in got
    assert T02_ANSWER_TEXT in got["text"]


def test_read_doc_unknown_id_returns_error(svc):
    """不存在的编号返回 error，而不是抛异常（模型要能看见并换一个编号）。"""
    got = svc.run_tool("read_doc", {"doc_id": "KB-999"})
    assert isinstance(got, dict)
    assert "error" in got


def test_read_doc_missing_required_param(svc):
    """守卫：缺必填参数时，参数校验必须先拦住。"""
    got = svc.run_tool("read_doc", {})
    assert "error" in got


# -- 契约守卫：search_kb 的对外结构不能被这次改动破坏 ---------------------------


def test_search_kb_payload_unchanged(svc):
    """守卫：/api/retrieve 与 search_kb 的 results 结构保持契约 §4 不变。"""
    got = svc.run_tool("search_kb", {"query": "退款政策", "top_k": 3})
    assert "results" in got
    assert len(got["results"]) == 3
    for hit in got["results"]:
        assert set(hit) == {"doc_id", "chunk_id", "score", "text"}
