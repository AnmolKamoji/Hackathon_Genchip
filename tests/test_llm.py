"""The AI layer must degrade to readable text, never raise into the dashboard.

A stub HTTP server stands in for Ollama and a fake SDK object for Anthropic, so
these run with no API key and no model installed.
"""
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ai import llm
from analyzer.fused import analyze_pair

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "data/samples"


# --------------------------------------------------------------- Ollama stub

class _Handler(BaseHTTPRequestHandler):
    reply = "Everything looks consistent."
    captured: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3:4b"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Handler.captured = json.loads(self.rfile.read(length))
        self._send({"message": {"role": "assistant", "content": _Handler.reply}})


@pytest.fixture
def stub_ollama(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    _Handler.reply = "Everything looks consistent."
    yield _Handler
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------- Anthropic stub

class FakeAnthropicError(Exception):
    pass


def install_fake_anthropic(monkeypatch, *, reply="Claude says hello.", stop_reason="end_turn",
                           raises=None, capture=None):
    """Install a fake `anthropic` module so no network call is made."""
    def _create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises is not None:
            raise raises
        blocks = [types.SimpleNamespace(type="text", text=reply)] if reply else []
        return types.SimpleNamespace(content=blocks, stop_reason=stop_reason,
                                     stop_details=types.SimpleNamespace(category="cyber")
                                     if stop_reason == "refusal" else None)

    class _Client:
        def __init__(self, **_):
            self.messages = types.SimpleNamespace(create=_create)
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=_create))

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Client
    fake.BadRequestError = FakeAnthropicError
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    return fake


@pytest.fixture(scope="module")
def metadata():
    return analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")


# ---------------------------------------------------------------- Anthropic

def test_anthropic_is_preferred_when_a_key_is_present(monkeypatch, metadata, stub_ollama):
    """A key must promote Anthropic ahead of the local model."""
    captured = {}
    install_fake_anthropic(monkeypatch, capture=captured)
    assert llm.provider_chain() == ["anthropic", "ollama"]
    assert llm.ask_llm(metadata, "Explain this layout.") == "Claude says hello."
    assert captured["model"] == "claude-opus-5"


def test_request_omits_parameters_that_error_on_opus_5(monkeypatch, metadata):
    """temperature / top_p / top_k / budget_tokens all return a 400 on this model."""
    captured = {}
    install_fake_anthropic(monkeypatch, capture=captured)
    llm.ask_llm(metadata, "Explain this layout.")
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in captured
    assert "budget_tokens" not in json.dumps(captured.get("thinking", {}))
    assert captured["output_config"]["effort"] == "low"
    assert captured["max_tokens"] == llm.DEFAULT_MAX_TOKENS


def test_metadata_is_a_cacheable_system_block(monkeypatch, metadata):
    """Metadata is the stable prefix and the question varies, so the cache
    breakpoint belongs after the metadata."""
    captured = {}
    install_fake_anthropic(monkeypatch, capture=captured)
    llm.ask_llm(metadata, "How many vias are present?")
    system = captured["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert "NR2D1" in system[-1]["text"]
    # The question must sit after the breakpoint, or every turn writes a new entry.
    assert "How many vias" not in json.dumps(system)
    assert "How many vias" in captured["messages"][0]["content"]


def test_refusal_is_reported_not_indexed(monkeypatch, metadata, stub_ollama):
    """A refusal can carry empty content; reading content[0] would raise."""
    install_fake_anthropic(monkeypatch, reply="", stop_reason="refusal")
    out = llm.ask_llm(metadata, "anything")
    # Falls through to the local model rather than surfacing a crash.
    assert out == "Everything looks consistent."


def test_fallback_opt_in_is_requested(monkeypatch, metadata):
    captured = {}
    install_fake_anthropic(monkeypatch, capture=captured)
    llm.ask_llm(metadata, "anything")
    assert captured["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["betas"]


def test_anthropic_failure_falls_back_to_local_model_with_metadata(monkeypatch, metadata, stub_ollama):
    """The regression that matters: the local model must still receive the
    metadata, which for Anthropic lives in the system block."""
    install_fake_anthropic(monkeypatch, raises=RuntimeError("connection reset"))
    out = llm.ask_llm(metadata, "How many polygons are there?")
    assert out == "Everything looks consistent."
    sent = stub_ollama.captured["messages"][1]["content"]
    assert "NR2D1" in sent and "polygon_count" in sent


def test_anthropic_only_does_not_fall_back(monkeypatch, metadata, stub_ollama):
    install_fake_anthropic(monkeypatch, raises=RuntimeError("boom"))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert llm.provider_chain() == ["anthropic"]
    out = llm.ask_llm(metadata, "anything")
    assert "No AI backend could answer" in out
    assert "boom" in out


def test_missing_sdk_is_reported_readably(monkeypatch, metadata):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
    out = llm.ask_llm(metadata, "anything")
    assert "anthropic" in out.lower()


def test_status_reports_the_chain(monkeypatch, stub_ollama):
    install_fake_anthropic(monkeypatch)
    status = llm.provider_status()
    assert status["ready"] is True
    assert "Anthropic" in status["primary"]
    assert status["chain"] == ["anthropic", "ollama"]


# ------------------------------------------------------------------- Ollama

def test_ollama_used_when_no_anthropic_key(stub_ollama, metadata):
    assert llm.provider_chain() == ["ollama"]
    assert llm.ask_llm(metadata, "Explain this layout to a non-expert.") == "Everything looks consistent."
    sent = stub_ollama.captured
    assert sent["model"] == "qwen3:4b"
    assert sent["stream"] is False
    assert sent["options"]["num_ctx"] == llm.DEFAULT_NUM_CTX


def test_output_is_capped(stub_ollama, metadata):
    """Without num_predict, qwen3 emitted >1400 tokens of preamble at ~7 tok/s."""
    llm.ask_llm(metadata, "summarise")
    assert stub_ollama.captured["options"]["num_predict"] == llm.DEFAULT_NUM_PREDICT


def test_reasoning_blocks_are_stripped(stub_ollama, metadata):
    stub_ollama.reply = "<think>let me count the layers</think>\nThe design has 60 polygons."
    assert llm.ask_llm(metadata, "summarise") == "The design has 60 polygons."


def test_unreachable_backend_returns_text_not_exception(monkeypatch, metadata):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    # Override the whole candidate list, not just OLLAMA_HOST: 127.0.0.1:11434 is
    # always a candidate, so on a developer machine with Ollama actually running
    # the request would reach the real server and this would test nothing.
    monkeypatch.setattr(llm, "candidate_hosts", lambda: ["http://127.0.0.1:1"])
    out = llm.ask_llm(metadata, "anything")
    assert "No AI backend could answer" in out
    assert llm.provider_status()["ready"] is False


def test_missing_model_is_reported_with_the_pull_command(stub_ollama, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.3:70b")
    assert "ollama pull llama3.3:70b" in llm.provider_status()["detail"]


def test_provider_none_disables_ai(monkeypatch, metadata):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert llm.provider_chain() == []
    assert llm.provider_status()["ready"] is False
    assert "disabled" in llm.ask_llm(metadata, "anything").lower()


# ------------------------------------------------------------- prompt sizing

def test_prompt_is_bounded(stub_ollama, metadata):
    """A real layout has thousands of cells; the prompt must stay inside 8k ctx."""
    fat = dict(metadata)
    fat["cells"] = [{"name": f"cell_{i}", "area_um2": float(i)} for i in range(5000)]
    fat["layers"] = metadata["layers"] * 50
    llm.ask_llm(fat, "summarise")
    assert len(stub_ollama.captured["messages"][1]["content"]) < llm.MAX_METADATA_CHARS + 2000


def test_digest_keeps_the_busiest_layers(metadata):
    digest = llm._digest(metadata)
    assert digest["design"] == metadata["design"]
    assert digest["consistency"] == metadata["consistency"]
    counts = [x.get("polygon_count") or 0 for x in digest["layers"]]
    assert counts == sorted(counts, reverse=True)


def test_authoritative_totals_reach_the_model(metadata):
    """`layer_groups.union_area_um2` is the only place the correct cross-layer
    area lives. It was being truncated out of the prompt, so the model could see
    only the per-row subsets and (correctly, per its rules) refused to total
    them - the analyzer fix never reached the answer.
    """
    text = llm._compact(metadata)
    parsed = json.loads(text)
    assert "layer_groups" in parsed
    group = next(g for g in parsed["layer_groups"] if g["label"] == "Diffusion_Break")
    assert group["union_area_um2"] == pytest.approx(0.01725)
    assert group["layer_numbers"] == [102, 103, 121]
    assert group["area_is_exclusive_to_this_name"] is False
    # And the model is told not to re-derive it.
    assert "do not" in parsed["layer_groups_note"].lower()


@pytest.mark.parametrize("blow_up", [
    lambda m: {**m, "layers": m["layers"] * 80},
    lambda m: {**m, "cells": [{"name": f"c{i}", "area_um2": float(i)} for i in range(9000)]},
    lambda m: {**m, "layer_groups": m["layer_groups"] * 40},
    lambda m: {**m, "layers": m["layers"] * 80, "layer_groups": m["layer_groups"] * 40,
               "cells": [{"name": f"c{i}"} for i in range(9000)]},
])
def test_oversized_metadata_still_yields_valid_json(metadata, blow_up):
    """Character truncation cut JSON mid-token, leaving the model to guess. The
    digest must shrink structurally instead, and keep the design-level facts."""
    text = llm._compact(blow_up(metadata))
    assert len(text) <= llm.MAX_METADATA_CHARS
    parsed = json.loads(text)                 # raises if malformed
    assert parsed["design"]["polygon_count"] == 60
    assert "layout" in parsed


def test_shrinking_sacrifices_rows_before_group_totals(metadata):
    """Per-layer rows are expendable; the per-name totals are not."""
    fat = {**metadata, "layers": metadata["layers"] * 80,
           "layer_groups": metadata["layer_groups"] * 40}
    parsed = json.loads(llm._compact(fat))
    assert parsed["layer_groups"]
    assert len(parsed.get("layers", [])) < len(fat["layers"])


def test_warnings_survive_the_digest(metadata):
    """A warning the analyzer raised must reach the model, or it will narrate
    unavailable facts as if they were measured."""
    fat = dict(metadata)
    fat["warnings"] = ["This GDS has 2 top-level cells."]
    assert llm._digest(fat)["warnings"] == fat["warnings"]


def test_comparison_digest_stays_valid_json_and_keeps_the_added_layers():
    """A blind character truncation cut off layers_added, the most useful field."""
    from analyzer.comparison import compare_metadata
    a = analyze_pair(SAMPLES / "NR2D1_1_RT_4.gds", SAMPLES / "NR2D1_1_RT_4.json")
    b = analyze_pair(SAMPLES / "NR2D1_2_RT_4.gds", SAMPLES / "NR2D1_2_RT_4.json")
    text = llm._compact({"comparison": compare_metadata(a, b)})
    assert len(text) < llm.MAX_METADATA_CHARS
    parsed = json.loads(text)["comparison"]      # raises if truncated mid-object
    assert "layer_changes" not in parsed         # the unbounded field is dropped
    assert {x["name"] for x in parsed["layers_added"]} >= {"M1", "VIA_M0_M1"}
    assert parsed["summary"]["polygon_delta"] == 7


def test_failure_detection_does_not_trip_on_bold_answers(monkeypatch, metadata):
    """A real answer can open with bold markdown ("**1. Headline:** ...").

    An earlier startswith("**") heuristic reported those as backend failures.
    """
    install_fake_anthropic(monkeypatch, reply="**1. Headline:** M1 was added.")
    reply = llm.ask_llm(metadata, "what changed")
    assert reply.startswith("**")
    assert not llm.looks_like_failure(reply)


def test_failure_detection_recognises_real_failures(monkeypatch, metadata, stub_ollama):
    install_fake_anthropic(monkeypatch, raises=RuntimeError("boom"))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert llm.looks_like_failure(llm.ask_llm(metadata, "anything"))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert llm.looks_like_failure(llm.ask_llm(metadata, "anything"))


def test_accuracy_rule_is_in_the_system_prompt():
    """The model must never be the source of a number."""
    from ai.prompts import SYSTEM_PROMPT
    assert "verbatim" in SYSTEM_PROMPT
    assert "null is NOT zero" in SYSTEM_PROMPT


# --------------------------------------------------------------------- hosts

def test_lan_router_is_never_offered_as_an_ollama_host(monkeypatch):
    """Under WSL mirrored networking the default route is the real router.

    Probing it would mean sending prompt data to an unrelated LAN device.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    hosts = llm.candidate_hosts()
    assert "http://127.0.0.1:11434" in hosts
    assert not any(h.startswith("http://192.168.") for h in hosts)


def test_bad_env_values_do_not_crash(stub_ollama, metadata, monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "not-a-number")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "")
    llm.ask_llm(metadata, "anything")
    assert stub_ollama.captured["options"]["num_ctx"] == llm.DEFAULT_NUM_CTX
