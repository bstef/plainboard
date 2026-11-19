"""Speaks the Messages interface to the agent harness and Chat Completions to a model server.

Most model servers and hosts (Gemini, Groq, Mistral, a llama.cpp of an older version ...) offer the
Chat Completions interface, the harness that runs an agent's turn speaks Anthropic's Messages
interface. This translates between the two, request and streamed answer, so the harness with all its
tools works on such a model. Nothing is stored but the notes a model wants back with its tool calls
(``_EXTRAS``). The functions are pure, so the mapping is tested without a server.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)
router = APIRouter()

# One request of a turn can take long: a local model reads a long prompt before its first word.
TIMEOUT = httpx.Timeout(connect=20, read=900, write=120, pool=20)
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use", "content_filter": "end_turn"}
ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error"}
# JSON Schema words some servers refuse (Gemini takes a part of the standard only). They describe a
# schema or tighten it; a tool's parameters are understood without them.
SCHEMA_NOISE = (
    "$schema", "$id", "$comment", "$defs", "definitions", "title", "examples", "default",
    "additionalProperties", "patternProperties", "propertyNames", "exclusiveMinimum", "exclusiveMaximum", "format",
)  # fmt: skip
# Gemini wants the signature of its thinking back with a tool call. For a call whose signature is not
# known any more (the runner was restarted in between), Google's documented word for "do not check".
GEMINI_HOST = "generativelanguage.googleapis.com"
NO_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}
# What a model wants back with a tool call it made (Gemini: the signature of its thinking), by the
# call's id. The harness cannot carry it: the Messages interface has no place for it.
_EXTRAS: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
EXTRAS_KEPT = 5000


def address(url: str) -> str:
    """A server's address as one path segment (see ``upstream``)."""
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def upstream(segment: str) -> str:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode().rstrip("/")


def _remember(call_id: str, extra: dict[str, Any] | None) -> None:
    if not extra:
        return
    _EXTRAS[call_id] = extra
    _EXTRAS.move_to_end(call_id)
    while len(_EXTRAS) > EXTRAS_KEPT:
        _EXTRAS.popitem(last=False)


def _text(content: Any) -> str:
    """The text of a Messages content: a string, or blocks of which the text ones count."""
    if isinstance(content, str):
        return content
    return "\n".join(str(block.get("text", "")) for block in content or [] if isinstance(block, dict) and block.get("type") == "text")


def _image(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"}}
    if source.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": source.get("url", "")}}
    return None


def _resolve(schema: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """A schema without references and without the words in SCHEMA_NOISE."""
    if isinstance(schema, list):
        return [_resolve(item, defs, depth) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and depth < 8:
        target = defs.get(ref.rsplit("/", 1)[-1])
        if target is not None:
            return _resolve({**target, **{k: v for k, v in schema.items() if k != "$ref"}}, defs, depth + 1)
    out = {}
    for key, value in schema.items():
        if key == "$ref" or key in SCHEMA_NOISE:
            continue
        if key == "const":
            out["enum"] = [value]
        elif key == "properties" and isinstance(value, dict):  # here the keys are names, not schema words
            out[key] = {name: _resolve(prop, defs, depth) for name, prop in value.items()}
        else:
            out[key] = _resolve(value, defs, depth)
    return out


def parameters(schema: dict[str, Any] | None) -> dict[str, Any]:
    """A tool's parameters as every server takes them: an object, self-contained."""
    schema = schema or {}
    out = _resolve(schema, {**(schema.get("definitions") or {}), **(schema.get("$defs") or {})})
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def _tool_choice(choice: dict[str, Any] | None) -> Any:
    kind = (choice or {}).get("type")
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return kind if kind in ("auto", "none") else None


def _joined(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the user says in a row, as one message: some models' chat forms want user and assistant in turn."""
    out: list[dict[str, Any]] = []
    for message in messages:
        last = out[-1] if out else None
        if last is None or message["role"] != "user" or last["role"] != "user":
            out.append(message)
        elif isinstance(last["content"], str) and isinstance(message["content"], str):
            last["content"] += f"\n\n{message['content']}"
        else:
            parts = lambda content: [{"type": "text", "text": content}] if isinstance(content, str) else content  # noqa: E731
            last["content"] = [*parts(last["content"]), *parts(message["content"])]
    return out


def to_chat(body: dict[str, Any], gemini: bool = False) -> dict[str, Any]:
    """A Messages request as a Chat Completions request. ``gemini``: for Google's server, which wants
    a signature with the first tool call of every answer."""
    messages: list[dict[str, Any]] = []
    if system := _text(body.get("system")):
        messages.append({"role": "system", "content": system})
    for message in body.get("messages") or []:
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
        if message.get("role") == "assistant":
            calls = []
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                call = {"id": block.get("id", ""), "type": "function", "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input") or {})}}
                if extra := _EXTRAS.get(call["id"]) or (NO_SIGNATURE if gemini and not calls else None):
                    call["extra_content"] = extra
                calls.append(call)
            text = _text(blocks)
            if text or calls:  # an answer of tool calls only has no content at all: some servers refuse an empty one
                messages.append({"role": "assistant", **({"content": text} if text else {}), **({"tool_calls": calls} if calls else {})})
            continue
        if message.get("role") == "system":
            # A note of the harness. Before the model's first answer it is part of the system prompt (the
            # harness puts the agent's surroundings there); later it is said in the talk. A chat has
            # one system message, at its start: many model servers refuse a second one.
            if not (text := _text(blocks)):
                continue
            if not any(m["role"] == "assistant" for m in messages):
                if messages and messages[0]["role"] == "system":
                    messages[0]["content"] += f"\n\n{text}"
                else:
                    messages.insert(0, {"role": "system", "content": text})
            else:
                messages.append({"role": "user", "content": f"[system]\n{text}"})
            continue
        # A user turn. The results of tool calls come first: they answer the assistant's message right before.
        parts: list[dict[str, Any]] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_result":
                inner = block.get("content")
                text = _text(inner) or ("" if isinstance(inner, list) else str(inner or ""))
                if block.get("is_error"):
                    text = f"Error: {text}"
                messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""), "content": text or "(no output)"})
                # A tool message is text only: what a tool shows (a screenshot) follows as the user's.
                parts += [image for inner_block in (inner if isinstance(inner, list) else []) if isinstance(inner_block, dict) and inner_block.get("type") == "image" and (image := _image(inner_block))]
            elif kind == "text" and block.get("text"):
                parts.append({"type": "text", "text": block["text"]})
            elif kind == "image" and (image := _image(block)):
                parts.append(image)
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            messages.append({"role": "user", "content": "\n".join(p["text"] for p in parts) if only_text else parts})

    out: dict[str, Any] = {"model": body.get("model", ""), "messages": _joined(messages), "stream": bool(body.get("stream"))}
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    for ours, theirs in (("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")):
        if body.get(ours) is not None:
            out[theirs] = body[ours]
    # Tools the harness asks the model's own host to run (a web search of Anthropic's) have no parameters: not here.
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") and "input_schema" in t]
    if tools:
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description") or "", "parameters": parameters(t["input_schema"])}} for t in tools]
        if (choice := _tool_choice(body.get("tool_choice"))) is not None:
            out["tool_choice"] = choice
    return out


def _usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {
        "input_tokens": max(0, int(usage.get("prompt_tokens") or 0) - cached),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def from_chat(answer: dict[str, Any], model: str) -> dict[str, Any]:
    """A whole Chat Completions answer as a Messages answer."""
    choice = (answer.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "text", "text": _text(message["content"])})
    for call in message.get("tool_calls") or []:
        call_id = call.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        _remember(call_id, call.get("extra_content"))
        function = call.get("function") or {}
        content.append({"type": "tool_use", "id": call_id, "name": function.get("name", ""), "input": _arguments(function.get("arguments"))})
    called = any(block["type"] == "tool_use" for block in content)
    return {
        "id": answer.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if called else STOP_REASONS.get(choice.get("finish_reason") or "stop", "end_turn"),
        "stop_sequence": None,
        "usage": _usage(answer.get("usage")),
    }


class Stream:
    """Turns the chunks of a Chat Completions stream into the events of a Messages stream."""

    def __init__(self, model: str, prompt_tokens: int = 0) -> None:
        """``prompt_tokens``: the size of the request, as far as known. A chat server says it at the
        end of its answer; the harness reads the size of its context at the start."""
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.started = False
        self.index = -1  # of the content block that is open
        self.open: str | None = None  # "text" or "tool_use"
        self.calls: dict[Any, str] = {}  # a chunk's name for a tool call -> the call's id
        self.current: str | None = None  # the id of the tool call that is being written
        self.called = False
        self.finish: str | None = None
        self.usage: dict[str, int] = _usage(None)

    def _start(self, message_id: str | None) -> list[dict[str, Any]]:
        if self.started:
            return []
        self.started = True
        usage = {**_usage(None), "input_tokens": self.prompt_tokens}
        message = {"id": message_id or f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        return [{"type": "message_start", "message": message}]

    def _close(self) -> list[dict[str, Any]]:
        if self.open is None:
            return []
        self.open = None
        return [{"type": "content_block_stop", "index": self.index}]

    def _block(self, kind: str, block: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._close()
        self.index += 1
        self.open = kind
        return [*events, {"type": "content_block_start", "index": self.index, "content_block": block}]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events = self._start(chunk.get("id"))
        if chunk.get("usage"):
            self.usage = _usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if text := delta.get("content"):
                if self.open != "text":
                    events += self._block("text", {"type": "text", "text": ""})
                events.append({"type": "content_block_delta", "index": self.index, "delta": {"type": "text_delta", "text": text if isinstance(text, str) else _text(text)}})
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                # A new call has a name. Servers number the calls of an answer, or send each whole with its id.
                key = call.get("index") if call.get("index") is not None else call.get("id")
                known = self.calls.get(key)
                fresh = bool(function.get("name")) and (known is None or (call.get("id") and call["id"] != known))
                if fresh:
                    call_id = call.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                    self.calls[key] = self.current = call_id
                    self.called = True
                    events += self._block("tool_use", {"type": "tool_use", "id": call_id, "name": function["name"], "input": {}})
                elif known is not None and known != self.current:
                    continue  # more of a call that is closed already: the events have no way back to it
                if self.current:
                    _remember(self.current, call.get("extra_content"))
                arguments = function.get("arguments")
                if arguments and self.open == "tool_use":
                    partial = arguments if isinstance(arguments, str) else json.dumps(arguments)
                    events.append({"type": "content_block_delta", "index": self.index, "delta": {"type": "input_json_delta", "partial_json": partial}})
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        return events

    def end(self) -> list[dict[str, Any]]:
        events = self._start(None) + self._close()
        reason = "tool_use" if self.called else STOP_REASONS.get(self.finish or "stop", "end_turn")
        return [*events, {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": self.usage}, {"type": "message_stop"}]


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def _error(status: int, message: str) -> dict[str, Any]:
    kind = ERROR_TYPES.get(status, "overloaded_error" if status in (503, 529) else "api_error")
    return {"type": "error", "error": {"type": kind, "message": message[:2000]}}


def _refusal(text: str) -> str:
    """What a server said when it refused, without the JSON around it."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text.strip() or "no answer"
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or (parsed.get("message") if isinstance(parsed, dict) else "") or text).strip()


def _headers(request: Request) -> dict[str, str]:
    """The key the harness was given, as the server wants it."""
    key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key and key != "none" else {})}


class Sizes:
    """Guesses how many tokens a request has, from its length and from what the server counted last time."""

    def __init__(self) -> None:
        self.per_char: dict[str, float] = {}

    @staticmethod
    def chars(chat: dict[str, Any]) -> int:
        return len(json.dumps(chat.get("messages") or [])) + len(json.dumps(chat.get("tools") or []))

    def guess(self, server: str, chat: dict[str, Any]) -> int:
        return int(self.chars(chat) * self.per_char.get(server, 0.27))

    def learn(self, server: str, chat: dict[str, Any], usage: dict[str, int]) -> None:
        counted = usage["input_tokens"] + usage["cache_read_input_tokens"]
        if counted and (chars := self.chars(chat)):
            self.per_char[server] = min(1.0, max(0.05, counted / chars))


sizes = Sizes()
# Servers and models that refused the answer length the harness asks for (another name for it, or a
# lower ceiling): asked without one, they take their own.
_NO_LENGTH: set[tuple[str, str]] = set()
LENGTH_WORDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maximum output", "output tokens")


async def _events(response: httpx.Response, client: httpx.AsyncClient, server: str, chat: dict[str, Any]) -> AsyncIterator[bytes]:
    stream = Stream(chat["model"], sizes.guess(server, chat))
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                yield _sse(_error(500, _refusal(data)))
                return
            for event in stream.feed(chunk):
                yield _sse(event)
        sizes.learn(server, chat, stream.usage)
        for event in stream.end():
            yield _sse(event)
    except httpx.HTTPError as exc:
        yield _sse(_error(500, f"The model server stopped answering: {exc}"))
    finally:
        await response.aclose()
        await client.aclose()


@router.post("/compat/{segment}/v1/messages")
async def messages(segment: str, request: Request):
    body = await request.json()
    base = upstream(segment)
    chat = to_chat(body, gemini=GEMINI_HOST in base)
    url = f"{base}/chat/completions"
    asked = (base, chat["model"])
    if asked in _NO_LENGTH:
        chat.pop("max_tokens", None)
    client = httpx.AsyncClient(timeout=TIMEOUT)
    while True:
        try:
            response = await client.send(client.build_request("POST", url, json=chat, headers=_headers(request)), stream=chat["stream"])
        except httpx.HTTPError as exc:
            await client.aclose()
            return JSONResponse(_error(502, f"The model server at {url} does not answer: {exc}"), status_code=502)
        if response.status_code < 400:
            break
        text = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        if response.status_code in (400, 422) and "max_tokens" in chat and any(word in text.lower() for word in LENGTH_WORDS):
            _NO_LENGTH.add(asked)
            del chat["max_tokens"]
            continue
        await client.aclose()
        log.info("Model server refused (%s): %s", response.status_code, text[:500])
        return JSONResponse(_error(response.status_code, _refusal(text)), status_code=response.status_code)
    if chat["stream"]:
        return StreamingResponse(_events(response, client, base, chat), media_type="text/event-stream")
    answer = response.json()
    await client.aclose()
    return JSONResponse(from_chat(answer, chat["model"]))


@router.post("/compat/{segment}/v1/messages/count_tokens")
async def count_tokens(segment: str, request: Request):
    """An estimate: these servers have no such question. The harness only sizes its context with it."""
    body = await request.json()
    return {"input_tokens": max(1, len(json.dumps(body.get("messages") or [])) // 4 + len(json.dumps(body.get("system") or "")) // 4 + len(json.dumps(body.get("tools") or [])) // 4)}
