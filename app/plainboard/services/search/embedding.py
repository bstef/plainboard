"""Embedding models: text in, unit vectors out.

``MODELS`` is the curated list the settings offer (ARCHITECTURE.md, "Semantic search", has the table and
where its numbers come from). All of them run on the app's CPU with onnxruntime, with no key and no cost;
their files are downloaded once into the data volume (pinned revision, checksums), in the background,
when the model first has work. Another kind of provider (an API with a key) is another ``Embedder`` with
a ``ModelCard`` of its own in ``MODELS``: every vector names the model that made it, so a change of model
embeds everything again instead of mixing (``orchestration/search_indexer.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ...config import get_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelCard:
    name: str  # stored with every vector
    label: str
    tier: str  # small | balanced | large
    description: str  # one line for the settings
    dim: int
    max_tokens: int
    licence: str
    download_mb: int
    ram_mb: int  # resident while loaded, measured
    speed: float  # chunks of ~300 tokens per second on 2 CPU threads, measured on the reference machine
    hint_similarity: float  # cosine from which the hint at the start of an assignment names a hit (calibrated per model)
    mmteb: float | None = None  # MTEB(Multilingual, v2): mean over tasks, and of its retrieval tasks
    mmteb_retrieval: float | None = None
    recommended: bool = False
    # Local ONNX models: where they come from and how to read them.
    repo: str = ""
    revision: str = ""
    files: dict[str, tuple[str, str]] = field(default_factory=dict)  # local name -> (path in the repository, sha256)
    pooling: str = "mean"  # mean | cls
    query_prefix: str = ""
    passage_prefix: str = ""


class Embedder(ABC):
    def __init__(self, card: ModelCard):
        self.card = card

    @property
    def name(self) -> str:
        return self.card.name

    @property
    @abstractmethod
    def state(self) -> str:
        """absent | downloading | loading | ready | failed"""

    @property
    def error(self) -> str:
        return ""

    @property
    def progress(self) -> float | None:
        """While downloading: the share done (0..1)."""
        return None

    @property
    def rate(self) -> float | None:
        """Chunks per second measured here, once it has embedded."""
        return None

    @abstractmethod
    def prepare(self) -> None:
        """Start making it ready (download, load) in the background; returns at once."""

    @abstractmethod
    async def passages(self, texts: list[str]) -> list[list[float]]:
        """Vectors of texts to be found."""

    @abstractmethod
    async def query(self, text: str) -> list[float]:
        """The vector of a search."""

    def unload(self) -> None:
        """Give its memory back (a model that is no longer used)."""


class LocalOnnxEmbedder(Embedder):
    """A sentence-embedding model in ONNX, run with onnxruntime: mean or first-token pooling, normalised."""

    def __init__(self, card: ModelCard):
        super().__init__(card)
        self._state, self._error = "absent", ""
        self._done = self._total = 0
        self._rate: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = asyncio.Lock()  # one batch at a time: the CPU is shared with everything else

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def progress(self) -> float | None:
        return self._done / self._total if self._state == "downloading" and self._total else None

    @property
    def rate(self) -> float | None:
        return self._rate

    @property
    def folder(self) -> Path:
        return get_config().data_dir / "models" / self.name

    def prepare(self) -> None:
        if self._state in ("ready", "downloading", "loading"):
            return
        self._state, self._error = "loading", ""
        self._task = asyncio.create_task(self._prepare(), name=f"embedder-{self.name}")

    async def _prepare(self) -> None:
        try:
            missing = {local: spec for local, spec in self.card.files.items() if not (self.folder / local).exists()}
            if missing:
                self._state, self._done, self._total = "downloading", 0, 0
                for local, (path, sha256) in missing.items():
                    await self._download(path, sha256, self.folder / local)
            self._state = "loading"
            await asyncio.to_thread(self._load)
            self._state = "ready"
            log.info("Embedding model %s is ready", self.name)
        except Exception as exc:  # tried again with the next work (prepare)
            self._state, self._error = "failed", str(exc)[:300]
            log.warning("Embedding model %s could not be made ready: %s", self.name, exc)

    async def _download(self, path: str, sha256: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        url = f"https://huggingface.co/{self.card.repo}/resolve/{self.card.revision}/{path}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._total += int(response.headers.get("content-length") or 0)
                with partial.open("wb") as out:
                    async for block in response.aiter_bytes(1 << 20):
                        digest.update(block)
                        out.write(block)
                        self._done += len(block)
        if digest.hexdigest() != sha256:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{path}: the download does not match its checksum")
        partial.replace(target)

    def _load(self) -> None:
        import onnxruntime
        import tokenizers

        tokenizer = tokenizers.Tokenizer.from_file(str(self.folder / "tokenizer.json"))
        tokenizer.enable_truncation(self.card.max_tokens)
        tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, min(2, (os.cpu_count() or 2) // 2))  # leaves the other cores to the app
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False  # the arena kept the largest batch's memory for good (GBs); without it, freed after each
        session = onnxruntime.InferenceSession(str(self.folder / "model.onnx"), options, providers=["CPUExecutionProvider"])
        self._tokenizer, self._session = tokenizer, session

    def _embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        encoded = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        inputs = {i.name for i in self._session.get_inputs()}
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        if hidden.ndim == 2:  # the model pools itself
            pooled = hidden
        elif self.card.pooling == "cls":
            pooled = hidden[:, 0]
        else:
            pooled = (hidden * mask[..., None]).sum(axis=1) / np.maximum(mask.sum(axis=1, keepdims=True), 1)
        pooled = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
        return pooled.astype(float).tolist()

    async def _run(self, texts: list[str]) -> list[list[float]]:
        if self._state != "ready":
            raise RuntimeError(f"The embedding model {self.name} is not ready")
        async with self._lock:
            started = time.monotonic()
            vectors = await asyncio.to_thread(self._embed, texts)
            if len(texts) > 1:
                rate = len(texts) / max(time.monotonic() - started, 1e-6)
                self._rate = rate if self._rate is None else 0.8 * self._rate + 0.2 * rate
            return vectors

    async def passages(self, texts: list[str]) -> list[list[float]]:
        return await self._run([self.card.passage_prefix + t for t in texts])

    async def query(self, text: str) -> list[float]:
        return (await self._run([self.card.query_prefix + text]))[0]

    def unload(self) -> None:
        self._session = self._tokenizer = None
        if self._state == "ready":
            self._state = "absent"


_HF = "https://huggingface.co"
_E5_QUERY, _E5_PASSAGE = "query: ", "passage: "  # e5 was trained with these: a search and what it should find are told apart

# Measured for PlainBoard (ARCHITECTURE.md, "Semantic search"): speed and memory on 2 threads, the hint's
# similarity at the share of real tickets whose nearest other ticket was about the same thing.
MODELS: tuple[ModelCard, ...] = (
    ModelCard(
        "multilingual-e5-small", "multilingual-e5-small", "small",
        "Small and fast: ~100 languages, the least memory and the quickest index; finds well, the larger ones find better.",
        dim=384, max_tokens=512, licence="MIT", download_mb=135, ram_mb=550, speed=8.0, hint_similarity=0.9, mmteb=55.5, mmteb_retrieval=49.3,
        repo="Xenova/multilingual-e5-small", revision="761b726dd34fb83930e26aab4e9ac3899aa1fa78",
        files={"model.onnx": ("onnx/model_quantized.onnx", "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
               "tokenizer.json": ("tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39")},
        query_prefix=_E5_QUERY, passage_prefix=_E5_PASSAGE,
    ),
    ModelCard(
        "gte-multilingual-base", "gte-multilingual-base", "balanced",
        "Recommended: clearly better at finding than e5 small (and e5 large), 70+ languages, long texts; about half as fast as small.",
        dim=768, max_tokens=8192, licence="Apache-2.0", download_mb=357, ram_mb=950, speed=3.8, hint_similarity=0.79, mmteb=58.2, mmteb_retrieval=56.5, recommended=True,
        repo="onnx-community/gte-multilingual-base", revision="2edbf5e672aab465f9ed4c154a8b61791c082c69",
        files={"model.onnx": ("onnx/model_quantized.onnx", "ab2bd164ebd8ca9003dc49a981b611e849b5d326f504c8873ba76e07fa6c0082"),
               "tokenizer.json": ("tokenizer.json", "3a56def25aa40facc030ea8b0b87f3688e4b3c39eb8b45d5702b3a1300fe2a20")},
        pooling="cls",
    ),
    ModelCard(
        "multilingual-e5-base", "multilingual-e5-base", "balanced",
        "The middle e5: ~100 languages, a little better than small, about 2.5x slower; gte-multilingual-base finds better at the same cost.",
        dim=768, max_tokens=512, licence="MIT", download_mb=296, ram_mb=750, speed=3.4, hint_similarity=0.86, mmteb=57.0, mmteb_retrieval=52.7,
        repo="Xenova/multilingual-e5-base", revision="1ec9243030a27d1a115d5c340572074c125b58b2",
        files={"model.onnx": ("onnx/model_quantized.onnx", "df7a9a29309e3ad491e1783adf8baee710262cc06079c7cbab63c630277fac94"),
               "tokenizer.json": ("tokenizer.json", "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626")},
        query_prefix=_E5_QUERY, passage_prefix=_E5_PASSAGE,
    ),
    ModelCard(
        "bge-m3", "bge-m3", "large",
        "Heavy: high quality, 100+ languages and long texts (8k tokens), about 7x slower than small; for a strong server.",
        dim=1024, max_tokens=8192, licence="MIT", download_mb=587, ram_mb=1200, speed=1.2, hint_similarity=0.71, mmteb=None, mmteb_retrieval=55.6,
        repo="Xenova/bge-m3", revision="4de13258303883538bd53b696b452bf8099f0858",
        files={"model.onnx": ("onnx/model_quantized.onnx", "0826f8c1ab9edf1801db86c61919d4d108e8bfc0b809ec823ad366882ff0b77d"),
               "tokenizer.json": ("tokenizer.json", "6710678b12670bc442b99edc952c4d996ae309a7020c1fa0096dd245c2faf790")},
        pooling="cls",
    ),
    ModelCard(
        "multilingual-e5-large", "multilingual-e5-large", "large",
        "Heavy: the largest e5, ~100 languages, about 7x slower than small; on retrieval not ahead of gte-multilingual-base.",
        dim=1024, max_tokens=512, licence="MIT", download_mb=579, ram_mb=1200, speed=1.2, hint_similarity=0.88, mmteb=58.6, mmteb_retrieval=54.1,
        repo="Xenova/multilingual-e5-large", revision="00fc3aeb3dbb95842de2ac1961d33c6319acf57b",
        files={"model.onnx": ("onnx/model_quantized.onnx", "0a8d65db9a36f810ba5da15249f13145fcdc7890e6656f1fd38cd8b7c4db1fca"),
               "tokenizer.json": ("tokenizer.json", "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626")},
        query_prefix=_E5_QUERY, passage_prefix=_E5_PASSAGE,
    ),
)  # fmt: skip
CARDS = {card.name: card for card in MODELS}
DEFAULT = "gte-multilingual-base"  # balanced: much better retrieval than e5 small for twice its time and memory

_current: dict[str, Embedder] = {}


def make(card: ModelCard) -> Embedder:
    """The embedder of a card. An API provider would be chosen here by a field of its card."""
    return LocalOnnxEmbedder(card)


def get(name: str | None = None) -> Embedder:
    """The embedder of that name (an unknown one: the default), made once per process."""
    name = name if name in CARDS else DEFAULT
    if name not in _current:
        _current[name] = make(CARDS[name])
    return _current[name]


def drop(name: str) -> None:
    """A model no longer in use gives its memory back."""
    if embedder := _current.pop(name, None):
        embedder.unload()
