"""Providers that are an address, and the translation between the two interfaces."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from plainboard_protocol import Credentials

from plainboard_runner import translate
from plainboard_runner.main import app
from plainboard_runner.providers import get_provider, list_providers
from plainboard_runner.providers.endpoints import ENDPOINTS, EndpointProvider


def test_every_endpoint_is_a_provider_with_what_the_settings_need():
    names = [p.name for p in list_providers()]
    assert len(names) == len(set(names))
    for endpoint in ENDPOINTS:
        info = get_provider(endpoint.name).info()
        assert info.endpoint is not None and info.label and info.efforts == []
        assert info.endpoint.url or not endpoint.models, f"{endpoint.name}: models without an address to ask them from"
        assert endpoint.url == endpoint.url.rstrip("/")
        # A Messages server is addressed without "/v1": the harness adds it.
        assert not (endpoint.api == "messages" and endpoint.url.endswith("/v1")), endpoint.name
    assert {"ollama", "gemini", "openrouter", "messages", "chat"} <= set(names)


def test_the_harness_is_pointed_at_the_account_s_address():
    ollama: EndpointProvider = get_provider("ollama")  # type: ignore[assignment]
    env = ollama.auth_env(Credentials(endpoint_url="http://models.lan:11434/", endpoint_context=64000), "qwen3-coder")
    assert env["ANTHROPIC_BASE_URL"] == "http://models.lan:11434" and env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "64000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "none" and env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["CLAUDE_CODE_SUBAGENT_MODEL"] == "qwen3-coder"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and "ENABLE_PROMPT_CACHING_1H" not in env
    # Without an address of its own: the provider's usual one.
    usual = ollama.auth_env(Credentials())
    assert usual["ANTHROPIC_BASE_URL"] == ollama.endpoint.url == "http://host.docker.internal:11434" and usual["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"

    # A server that speaks Chat Completions is reached through the runner's translation, the key as it is.
    gemini: EndpointProvider = get_provider("gemini")  # type: ignore[assignment]
    env = gemini.auth_env(Credentials(endpoint_key="g-key"), "gemini-pro")
    prefix = "http://127.0.0.1:8090/compat/"
    assert env["ANTHROPIC_BASE_URL"].startswith(prefix) and env["ANTHROPIC_AUTH_TOKEN"] == "g-key"
    assert translate.upstream(env["ANTHROPIC_BASE_URL"].removeprefix(prefix)) == gemini.endpoint.url
    with pytest.raises(RuntimeError, match="no API key"):
        gemini.auth_env(Credentials())
    with pytest.raises(RuntimeError, match="no address"):
        get_provider("chat").auth_env(Credentials(endpoint_key="k"))  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="http://"):
        get_provider("chat").auth_env(Credentials(endpoint_key="k", endpoint_url="localhost:8000"))  # type: ignore[attr-defined]


def test_a_messages_request_becomes_a_chat_request():
    body = {
        "model": "m",
        "max_tokens": 4000,
        "stream": True,
        "system": [{"type": "text", "text": "You are an agent.", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "Be brief."}],
        "tool_choice": {"type": "any"},
        "tools": [
            {
                "name": "read",
                "description": "Reads a file",
                "input_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"path": {"type": "string", "title": "Path", "format": "uri"}, "range": {"$ref": "#/$defs/Range"}, "title": {"type": "string"}, "kind": {"const": "file"}},
                    "required": ["path"],
                    "additionalProperties": False,
                    "$defs": {"Range": {"type": "object", "properties": {"start": {"type": "integer", "default": 0}}}},
                },
            },
            {"type": "web_search_20250305", "name": "web_search"},  # run by Anthropic's servers: not offered here
        ],
        "messages": [
            {"role": "user", "content": "Read a.txt"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "I read it."}, {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a.txt"}}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "hello"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
                    {"type": "text", "text": "And now b.txt"},
                ],
            },
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "read", "input": {"path": "b.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "No such file", "is_error": True}]},
        ],
    }
    chat = translate.to_chat(body)
    assert chat["stream"] and chat["stream_options"] == {"include_usage": True} and chat["max_tokens"] == 4000 and chat["tool_choice"] == "required"
    assert [t["function"]["name"] for t in chat["tools"]] == ["read"]
    parameters = chat["tools"][0]["function"]["parameters"]
    assert parameters == {
        "type": "object",
        # A property may be called like a schema word ("title"): it stays. The words themselves go, references are resolved.
        "properties": {"path": {"type": "string"}, "range": {"type": "object", "properties": {"start": {"type": "integer"}}}, "title": {"type": "string"}, "kind": {"enum": ["file"]}},
        "required": ["path"],
    }
    roles = [(m["role"], m.get("tool_call_id")) for m in chat["messages"]]
    assert roles == [("system", None), ("user", None), ("assistant", None), ("tool", "t1"), ("user", None), ("assistant", None), ("tool", "t2")]
    assert chat["messages"][0]["content"] == "You are an agent.\nBe brief."
    assert chat["messages"][2]["content"] == "I read it." and chat["messages"][2]["tool_calls"][0] == {"id": "t1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.txt"}'}}
    assert chat["messages"][3]["content"] == "hello"
    # What a tool showed follows its result as the user's: a tool message is text only.
    assert chat["messages"][4]["content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}, {"type": "text", "text": "And now b.txt"}]
    # An answer of tool calls only carries no content at all.
    assert "content" not in chat["messages"][5] and chat["messages"][6]["content"] == "Error: No such file"


def test_the_harness_s_notes_and_messages_in_a_row_fit_a_chat():
    """Seen with the real harness: it sends the agent's surroundings as a system entry among the
    messages, and two user messages in a row. A chat has one system message at its start, and many
    models' chat forms want user and assistant in turn."""
    talk = [
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>Commit as you like.</system-reminder>"}, {"type": "text", "text": "Read note.txt"}]},
        {"role": "system", "content": "# Environment\nYou work in /workspaces/a."},
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        {"role": "user", "content": "Thanks."},
        {"role": "system", "content": "The date changed."},
        {"role": "user", "content": [{"type": "text", "text": "Look at this"}, {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]},
    ]
    chat = translate.to_chat({"model": "m", "system": "You are an agent.", "messages": talk})
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant", "user"]
    assert chat["messages"][0]["content"] == "You are an agent.\n\n# Environment\nYou work in /workspaces/a."
    assert chat["messages"][1]["content"] == "<system-reminder>Commit as you like.</system-reminder>\nRead note.txt"
    assert chat["messages"][3]["content"] == [
        {"type": "text", "text": "Thanks.\n\n[system]\nThe date changed."},
        {"type": "text", "text": "Look at this"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]
    # Without a system prompt of its own, the note is the system message.
    assert translate.to_chat({"model": "m", "messages": talk[:2]})["messages"][0] == {"role": "system", "content": "# Environment\nYou work in /workspaces/a."}


def events_of(chunks: list[dict], model: str = "m") -> list[dict]:
    stream = translate.Stream(model)
    return [event for chunk in chunks for event in stream.feed(chunk)] + stream.end()


def test_a_chat_stream_becomes_the_events_of_a_messages_stream():
    events = events_of(
        [
            {"id": "c1", "choices": [{"delta": {"role": "assistant", "content": "Let me "}}]},
            {"id": "c1", "choices": [{"delta": {"content": "look."}}]},
            {"id": "c1", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "read", "arguments": ""}}]}}]},
            {"id": "c1", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}}]},
            {"id": "c1", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "a.txt"}'}}]}}]},
            {"id": "c1", "choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "list", "arguments": "{}"}}]}}]},
            {"id": "c1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"id": "c1", "choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 100}}},
        ]
    )
    kinds = [e["type"] for e in events]
    assert kinds == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]  # fmt: skip
    assert events[0]["message"]["id"] == "c1" and events[0]["message"]["model"] == "m"
    assert [e["index"] for e in events if e["type"] == "content_block_start"] == [0, 1, 2]
    assert events[5]["content_block"] == {"type": "tool_use", "id": "call_a", "name": "read", "input": {}}
    written = "".join(e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta" and e["index"] == 1)
    assert json.loads(written) == {"path": "a.txt"}
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    assert events[-2]["usage"] == {"input_tokens": 20, "output_tokens": 30, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}

    # Text only, and an answer that was cut.
    assert events_of([{"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}])[-2]["delta"]["stop_reason"] == "end_turn"
    assert events_of([{"choices": [{"delta": {"content": "Do"}, "finish_reason": "length"}]}])[-2]["delta"]["stop_reason"] == "max_tokens"
    # No words at all: still a complete answer.
    assert [e["type"] for e in events_of([])] == ["message_start", "message_delta", "message_stop"]


def test_tool_calls_that_come_whole_and_unnumbered_are_told_apart_and_keep_what_the_model_wants_back():
    """Gemini sends each tool call in one piece, and with it the signature of its thinking, which it
    wants back with the call in the next request. A server that calls a tool may also say "stop"."""
    signature = {"google": {"thought_signature": "sig-1"}}
    events = events_of(
        [
            {"choices": [{"delta": {"tool_calls": [{"id": "g1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a"}'}, "extra_content": signature}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"id": "g2", "type": "function", "function": {"name": "read", "arguments": '{"path": "b"}'}}]}, "finish_reason": "stop"}]},
        ]
    )
    starts = [e["content_block"] for e in events if e["type"] == "content_block_start"]
    assert [(b["id"], b["name"]) for b in starts] == [("g1", "read"), ("g2", "read")]
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    back = translate.to_chat({"model": "m", "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "g1", "name": "read", "input": {"path": "a"}}, {"type": "tool_use", "id": "g2", "name": "read", "input": {"path": "b"}}]}]})
    calls = back["messages"][0]["tool_calls"]
    assert calls[0]["extra_content"] == signature and "extra_content" not in calls[1]
    # A call whose signature is gone (the runner was restarted): for Google's server the word that skips the check,
    # on the first call of the answer, and nothing of the kind for any other server.
    lost = {"model": "m", "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "x1", "name": "read", "input": {}}, {"type": "tool_use", "id": "x2", "name": "read", "input": {}}]}]}
    calls = translate.to_chat(lost, gemini=True)["messages"][0]["tool_calls"]
    assert calls[0]["extra_content"] == translate.NO_SIGNATURE and "extra_content" not in calls[1]
    assert "extra_content" not in translate.to_chat(lost)["messages"][0]["tool_calls"][0]


def test_a_whole_chat_answer_becomes_a_messages_answer():
    answer = {
        "id": "c9",
        "choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": [{"id": "", "function": {"name": "read", "arguments": '{"path": "a"}'}}]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2},
    }
    message = translate.from_chat(answer, "m")
    assert message["stop_reason"] == "tool_use" and message["usage"]["input_tokens"] == 9
    assert message["content"][0] == {"type": "text", "text": "ok"}
    assert message["content"][1]["input"] == {"path": "a"} and message["content"][1]["id"].startswith("call_")


class FakeServer:
    """A Chat Completions server, in place of the network."""

    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status, self.body, self.requests = status, body, []

    def install(self, monkeypatch) -> "FakeServer":
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, content=self.body)

        real = httpx.AsyncClient
        monkeypatch.setattr(translate.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
        return self


def test_the_runner_translates_a_turn_s_request_and_its_streamed_answer(monkeypatch):
    chunks = [
        {"id": "c1", "choices": [{"delta": {"content": "Hi"}}]},
        {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
    ]
    server = FakeServer(body="".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n").install(monkeypatch)
    segment = translate.address("https://models.example/v1")
    with TestClient(app) as client:
        response = client.post(f"/compat/{segment}/v1/messages?beta=true", json={"model": "m", "stream": True, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]}, headers={"Authorization": "Bearer the-key"})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [e["type"] for e in events] == ["message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]
        assert events[2]["delta"]["text"] == "Hi" and events[-2]["usage"]["input_tokens"] == 5
        sent = server.requests[0]
        assert str(sent.url) == "https://models.example/v1/chat/completions" and sent.headers["authorization"] == "Bearer the-key"
        assert json.loads(sent.content)["messages"] == [{"role": "user", "content": "Hi"}]
        assert client.post(f"/compat/{segment}/v1/messages/count_tokens", json={"messages": [{"role": "user", "content": "Hi there"}]}).json()["input_tokens"] >= 1


def test_the_size_of_the_prompt_is_said_at_the_start_and_a_refused_answer_length_is_left_out(monkeypatch):
    """A chat server counts the prompt at the end of its answer, the harness reads its context's size
    at the start: a guess from the request's length, corrected by what the server counted. And a
    server that refuses the answer length the harness asks for is asked again without one."""
    monkeypatch.setattr(translate, "sizes", translate.Sizes())
    monkeypatch.setattr(translate, "_NO_LENGTH", set())
    chunks = [{"id": "c1", "choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4000, "completion_tokens": 1}}]
    answer = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "max_tokens is too large: 32000. This model supports at most 8192 output tokens."}})
        return httpx.Response(200, content=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(translate.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    segment = translate.address("https://models.example/v1")
    request = {"model": "m", "stream": True, "max_tokens": 32000, "messages": [{"role": "user", "content": "x" * 10000}]}

    def start_size(client) -> int:
        response = client.post(f"/compat/{segment}/v1/messages", json=request)
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-2]["usage"]["input_tokens"] == 4000
        return events[0]["message"]["usage"]["input_tokens"]

    with TestClient(app) as client:
        first = start_size(client)
        assert 2000 < first < 3500  # a guess from the length alone
        assert ["max_tokens" in b for b in bodies] == [True, False]
        second = start_size(client)
        assert 3900 < second < 4100  # as the server counted the same request before
        assert ["max_tokens" in b for b in bodies] == [True, False, False]  # not asked for again


def test_what_a_model_server_refuses_reaches_the_harness_as_an_error_it_understands(monkeypatch):
    FakeServer(status=429, body=json.dumps({"error": {"message": "Quota exceeded for this model", "code": 429}}).encode()).install(monkeypatch)
    with TestClient(app) as client:
        response = client.post(f"/compat/{translate.address('https://models.example/v1')}/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 429
    assert response.json() == {"type": "error", "error": {"type": "rate_limit_error", "message": "Quota exceeded for this model"}}


async def test_the_models_a_server_offers_are_read_from_it(monkeypatch):
    from plainboard_runner.providers import endpoints

    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/v1/models":
            return httpx.Response(404, text="not here")
        return httpx.Response(200, json={"data": [{"id": "models/b-model"}, {"id": "a-model"}, {"id": "a-model"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(endpoints.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    # A Messages server: asked with and without "/v1", whichever it has.
    assert await get_provider("messages").models(Credentials(endpoint_url="https://gateway.example", endpoint_key="k")) == ["a-model", "b-model"]
    assert asked == [("https://gateway.example/v1/models", "Bearer k"), ("https://gateway.example/models", "Bearer k")]
    with pytest.raises(RuntimeError, match="no address"):
        await get_provider("chat").models(Credentials())
