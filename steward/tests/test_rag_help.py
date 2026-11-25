import asyncio
import hashlib
import math
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from steward_agent.agent import Agent
from steward_agent.config import AgentConfig
from steward_agent.help_api import SlidingWindow, create_help_app
from steward_agent.rag import chunk_markdown


def run(coro):
    return asyncio.run(coro)


def bow_vector(text: str, dims: int = 4096) -> list[float]:
    """Deterministic bag-of-words embedding: shared words -> high cosine."""
    v = [0.0] * dims
    for w in re.findall(r"[a-z]{3,}", text.lower().replace("search_query", "").replace("search_document", "")):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dims] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


class FakeLLM:
    def __init__(self, reply="Open the app and sign in."):
        self.reply = reply
        self.chats = []
        self.embed_calls = 0

    async def embed(self, model, inputs, options=None):
        self.embed_calls += 1
        return [bow_vector(t) for t in inputs]

    async def chat(self, model, messages, tools, options=None):
        self.chats.append({"messages": messages, "tools": tools, "options": options})
        return {"content": self.reply}

    async def is_loaded(self, model):
        return True


HELP_DOC = """# Getting connected

## Add a new device to the VPN
Install the VPN app on the device, sign in with the account the admin invited, and approve it.

## Watching movies
Open the media app and sign in with your media account.
"""

ADMIN_DOC = """# Torrent stack notes
Restarting the torrent stack interrupts active downloads.
"""


def make_agent(tmp_path, reply="Install the VPN app and sign in.\nSources: 1", **cfg):
    (tmp_path / "docs/help").mkdir(parents=True)
    (tmp_path / "docs/admin").mkdir(parents=True)
    (tmp_path / "docs/help/connect.md").write_text(HELP_DOC)
    (tmp_path / "docs/admin/torrent.md").write_text(ADMIN_DOC)
    conf = AgentConfig.model_validate({"rag": {"min_score": 0.3}, "help": cfg})
    llm = FakeLLM(reply)
    agent = Agent(lambda: conf, llm, executor=None, tz=ZoneInfo("UTC"),
                  docs_dir=tmp_path / "docs", index_dir=tmp_path / "index")
    run(agent.refresh_indexes())
    return agent, llm


# ---------------------------------------------------------------- chunking
def test_chunker_keeps_heading_path_and_ignores_code_fences():
    md = "# Guide\nintro\n## Setup\nstep one\n```\n# not a heading\n```\n### Detail\nmore\n"
    chunks = chunk_markdown(md, "guide.md")
    titles = [c.title for c in chunks]
    assert titles == ["Guide", "Guide > Setup", "Guide > Setup > Detail"]
    assert "# not a heading" in chunks[1].text


def test_chunker_splits_long_sections():
    md = "# Big\n" + "\n\n".join("word " * 100 for _ in range(10))
    chunks = chunk_markdown(md, "big.md", max_chars=1200)
    assert len(chunks) > 1 and all(len(c.text) <= 1200 for c in chunks)


def test_title_falls_back_to_filename():
    assert chunk_markdown("just text", "add-a-device.md")[0].title == "Add a device"


# ---------------------------------------------------------------- index
def test_index_is_cached_and_rebuilt_on_change(tmp_path):
    agent, llm = make_agent(tmp_path)
    calls = llm.embed_calls
    run(agent.refresh_indexes())
    assert llm.embed_calls == calls                       # nothing changed: no re-embedding
    (tmp_path / "docs/help/new.md").write_text("# Printing\nUse the printer app.")
    run(agent.refresh_indexes())
    assert llm.embed_calls > calls and agent.indexes["help"].size == 3


def test_index_survives_restart_without_reembedding(tmp_path):
    agent, llm = make_agent(tmp_path)
    fresh = Agent(agent._cfg_loader, llm, None, ZoneInfo("UTC"),
                  docs_dir=tmp_path / "docs", index_dir=tmp_path / "index")
    before = llm.embed_calls
    run(fresh.refresh_indexes())
    assert llm.embed_calls == before and fresh.indexes["help"].size == 2


def test_collections_are_separate(tmp_path):
    agent, _ = make_agent(tmp_path)
    hits = run(agent.retrieve("help", "restarting the torrent stack downloads"))
    assert all("Torrent" not in h.title for h in hits)


# ---------------------------------------------------------------- help answers
def test_help_answers_from_docs_with_sources_and_no_tools(tmp_path):
    agent, llm = make_agent(tmp_path)
    res = run(agent.answer_help("How do I add a new device to the VPN?", "h1"))
    assert res.reason == "answered" and not res.forwarded
    assert res.sources[0]["title"] == "Getting connected > Add a new device to the VPN"
    assert llm.chats[0]["tools"] == []                    # help path never sees tools
    assert "<question>" in llm.chats[0]["messages"][1]["content"]


def test_help_forwards_when_nothing_relevant(tmp_path):
    agent, llm = make_agent(tmp_path)
    res = run(agent.answer_help("What's your favourite pizza topping?", "h2"))
    assert res.reason == "no_docs" and res.forwarded and llm.chats == []


@pytest.mark.parametrize("reply", ["UNSURE", "unsure.", "**UNSURE**", ""])
def test_help_forwards_when_model_unsure(tmp_path, reply):
    agent, _ = make_agent(tmp_path, reply=reply)
    res = run(agent.answer_help("How do I add a new device to the VPN?", "h3"))
    assert res.reason == "unsure" and res.forwarded and res.sources == []


def test_forwarding_can_be_disabled(tmp_path):
    agent, _ = make_agent(tmp_path, forward_unanswered=False)
    res = run(agent.answer_help("pizza toppings?", "h4"))
    assert not res.forwarded and "Please contact" in res.answer


# ---------------------------------------------------------------- admin retrieval
def test_admin_plan_includes_notes_only_when_relevant(tmp_path):
    agent, llm = make_agent(tmp_path, reply="Restarting interrupts downloads.")
    agent.tools = {}
    run(agent.plan("what happens if I restart the torrent stack downloads", "a1"))
    msgs = llm.chats[-1]["messages"]
    assert len(msgs) == 3 and "Torrent stack notes" in msgs[1]["content"]
    assert "information only" in msgs[1]["content"]
    run(agent.plan("pizza", "a2"))
    assert len(llm.chats[-1]["messages"]) == 2


# ---------------------------------------------------------------- HTTP
def test_http_help_and_forwarding(tmp_path):
    agent, _ = make_agent(tmp_path)
    forwarded = []

    async def forward(text):
        forwarded.append(text)

    client = TestClient(create_help_app(agent, forward))
    r = client.post("/help", json={"question": "How do I add a new device to the VPN?"})
    assert r.status_code == 200 and r.json()["sources"][0]["title"].endswith("VPN")
    assert "score" not in r.json()["sources"][0]          # internals stay internal
    r = client.post("/help", json={"question": "pizza toppings?"})
    assert r.json()["forwarded"] is True and "pizza" in forwarded[0]
    assert client.post("/help", json={"question": "  "}).status_code == 400
    assert client.post("/help", json={"question": "x" * 600}).status_code == 400


def test_http_rate_limit_per_ip(tmp_path):
    agent, _ = make_agent(tmp_path, rate_limit_per_minute=2)

    async def forward(text):
        pass

    client = TestClient(create_help_app(agent, forward))
    q = {"question": "How do I add a new device?"}
    codes = [client.post("/help", json=q, headers={"X-Forwarded-For": "100.64.0.5"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    other = client.post("/help", json=q, headers={"X-Forwarded-For": "100.64.0.6"})
    assert other.status_code == 200


def test_sliding_window():
    t = [0.0]
    w = SlidingWindow(2, 60, clock=lambda: t[0])
    assert w.allow("a") and w.allow("a") and not w.allow("a")
    t[0] = 61
    assert w.allow("a")


# ---------------------------------------------------------------- status + contact form
def test_help_status_and_answered_flag(tmp_path):
    agent, _ = make_agent(tmp_path)

    async def forward(text):
        pass

    client = TestClient(create_help_app(agent, forward))
    assert client.get("/help/status").json() == {"ready": True}
    r = client.post("/help", json={"question": "How do I add a new device to the VPN?"})
    assert r.json()["answered"] is True
    r = client.post("/help", json={"question": "pizza toppings?"})
    assert r.json()["answered"] is False


def test_contact_form_relays_escaped_text(tmp_path):
    agent, llm = make_agent(tmp_path)
    sent = []

    async def forward(text):
        sent.append(text)

    client = TestClient(create_help_app(agent, forward))
    r = client.post("/message", json={"name": "Bob", "contact": "@everyone",
                                      "message": "**Roku** isn't working\nsecond line"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "Bob" in sent[0] and "\\*\\*Roku\\*\\*" in sent[0]
    assert "@everyone" not in sent[0] and "> second line" in sent[0]
    assert llm.chats == []                                # contact form never touches the model
    assert client.post("/message", json={"name": "", "message": "hi"}).status_code == 400
    assert client.post("/message", json={"name": "x", "message": "y" * 2001}).status_code == 400


def test_contact_form_rate_limit_and_failure(tmp_path):
    agent, _ = make_agent(tmp_path, contact_messages_per_hour=1)

    async def broken(text):
        raise RuntimeError("discord down")

    client = TestClient(create_help_app(agent, broken))
    r = client.post("/message", json={"name": "A", "message": "hi"})
    assert r.status_code == 502 and "another way" in r.json()["error"]
    assert client.post("/message", json={"name": "A", "message": "hi"}).status_code == 429


# ---------------------------------------------------------------- grounding (live findings 2026-11-23)
from steward_agent.grounding import cited_sources, ground_urls

DOCS = "Jellyfin: https://jellyfin.home.example.com\nPhotos: https://photos.home.example.com"


def test_garbled_url_is_corrected():
    out, fixes, ok = ground_urls("Open https://jellyin.home.example.com.", DOCS)
    assert ok and out == "Open https://jellyfin.home.example.com."
    assert fixes == [("https://jellyin.home.example.com", "https://jellyfin.home.example.com")]


def test_bare_host_from_docs_is_fine():
    out, fixes, ok = ground_urls("Use photos.home.example.com in the app", DOCS)
    assert ok and not fixes


def test_invented_address_is_rejected():
    _, _, ok = ground_urls("Go to https://netflix.com/login", DOCS)
    assert not ok


def test_sources_line_parsed_and_stripped():
    hits = ["a", "b", "c"]
    body, cited = cited_sources("Use Swiftfin.\nSources: 3, 1, 3, 9", hits)
    assert body == "Use Swiftfin." and cited == ["c", "a"]
    assert cited_sources("I can't share that.\nSources: none", hits) == ("I can't share that.", [])
    assert cited_sources("No sources line", hits) == ("No sources line", [])
    assert cited_sources("Answer\n**Sources:** 2", hits)[1] == ["b"]


def test_help_answer_fixes_url_and_shows_only_cited(tmp_path):
    doc_reply = ("Install the VPN app on https://tailscle.com/download and sign in.\nSources: 1")
    agent, _ = make_agent(tmp_path, reply=doc_reply)
    (tmp_path / "docs/help/connect.md").write_text(HELP_DOC.replace(
        "Install the VPN app", "Install the VPN app from https://tailscale.com/download"))
    run(agent.refresh_indexes())
    res = run(agent.answer_help("How do I add a new device to the VPN?", "g1"))
    assert res.reason == "answered" and "https://tailscale.com/download" in res.answer
    assert "Sources" not in res.answer and len(res.sources) == 1


def test_help_refusal_shows_no_sources(tmp_path):
    agent, _ = make_agent(tmp_path, reply="I can't share that.\nSources: none")
    res = run(agent.answer_help("How do I add a new device to the VPN?", "g2"))
    assert res.reason == "answered" and res.sources == []


def test_help_with_invented_url_is_not_shown(tmp_path):
    agent, _ = make_agent(tmp_path, reply="Go to https://evil.example.org now.\nSources: 1")
    res = run(agent.answer_help("How do I add a new device to the VPN?", "g3"))
    assert res.reason == "unsure" and "evil" not in res.answer
