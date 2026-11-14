"""The embedders memory uses, and the guard that the query embedder matches
the stored episodes.

Two providers:

  voyage  Voyage models through the Atlas Embedding and Reranking API
          (https://ai.mongodb.com/v1) or api.voyageai.com. Needs EMBED_API_KEY
          (or VOYAGE_API_KEY).
  hash    Deterministic local embedding, no network, no key. Identical text
          gives cosine 1.0 and shared wording gives a higher score, which is
          all the memory ordering needs. Used when no provider is configured.

Profiles are embedded without an input_type on both the stored side and the
query side, so the same profile text always maps to the same vector.

On top of the provider:

* One embedder per process per provider and model, wrapped in a small text
  memo. A party's profile is embedded every time one of its attempts ends
  (the episode) and again before its first call in the next recall (the
  query). The demo has four profiles, so the memo turns dozens of Voyage
  round trips into four, which keeps the worker inside the free tier's rate
  limit and keeps a memory check off the network once a profile is known.
* check_embedder(): retrieval compares the query vector with stored vectors,
  so the query must come from the model the stored episodes were embedded
  with. A different model gives scores that rank nothing, and nothing would
  say so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import urllib.request
from collections import OrderedDict

log = logging.getLogger(__name__)

# What a failing embedding call raises: urllib's URLError/HTTPError and
# timeouts are OSErrors, a malformed body is a ValueError (JSON) or a
# KeyError (missing "data"). Callers that must not stop on a provider outage
# (the worker's memory check and episode writer) catch exactly these.
EMBED_ERRORS = (OSError, ValueError, KeyError)

_DIMS_PROBE = "dimension probe"
_lock = threading.Lock()
_embedders: dict[tuple, "CachedEmbedder"] = {}


_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """256 signed hash buckets over words and word pairs, L2-normalised."""
    name = "hash-256"
    dims = 256

    def embed(self, texts) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        vec = [0.0] * self.dims
        for f in feats:
            h = int(hashlib.sha1(f.encode("utf-8")).hexdigest()[:10], 16)
            vec[h % self.dims] += 1.0 if (h >> 20) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class VoyageEmbedder:
    """Voyage's /embeddings endpoint, 64 texts per request."""

    def __init__(self, api_key: str, model: str = "voyage-4", base_url: str = "https://ai.mongodb.com/v1",
                 output_dimension=None, timeout: float = 30):
        self.api_key, self.model = api_key, model
        self.base_url = base_url.rstrip("/")
        self.output_dimension = output_dimension
        self.timeout = timeout
        self.name = model
        self.dims = None  # known after the first call

    def embed(self, texts) -> list[list[float]]:
        texts = list(texts)
        out = []
        for i in range(0, len(texts), 64):
            body = {"input": texts[i:i + 64], "model": self.model}
            if self.output_dimension:
                body["output_dimension"] = int(self.output_dimension)
            req = urllib.request.Request(
                f"{self.base_url}/embeddings", data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())["data"]
            out.extend(row["embedding"] for row in sorted(data, key=lambda r: r["index"]))
        if out:
            self.dims = len(out[0])
        return out


def make_embedder(kind: str | None = None, env=None):
    """A fresh provider client. EMBED_PROVIDER wins; otherwise voyage when a
    key is set, else hash. Raises SystemExit for voyage without a key or an
    unknown provider: a misconfigured process must not start."""
    env = os.environ if env is None else env
    key = env.get("EMBED_API_KEY") or env.get("VOYAGE_API_KEY")
    kind = kind or env.get("EMBED_PROVIDER") or ("voyage" if key else "hash")
    if kind == "hash":
        return HashEmbedder()
    if kind == "voyage":
        if not key:
            raise SystemExit("EMBED_PROVIDER=voyage needs EMBED_API_KEY (or VOYAGE_API_KEY)")
        return VoyageEmbedder(key, model=env.get("EMBED_MODEL", "voyage-4"),
                              base_url=env.get("EMBED_BASE_URL", "https://ai.mongodb.com/v1"),
                              output_dimension=env.get("EMBED_DIMS"))
    raise SystemExit(f"unknown EMBED_PROVIDER {kind!r}: use voyage or hash")


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class CachedEmbedder:
    """An embedder that remembers the vectors of the last `maxsize` texts.

    `name` and `dims` are the wrapped embedder's, so episodes and the
    preflight see the real model. Thread-safe: the worker embeds from
    several threads at once.
    """

    def __init__(self, inner, maxsize: int = 256):
        self.inner = inner
        self.name = inner.name
        self.maxsize = maxsize
        self._memo: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def dims(self) -> int | None:
        """Known up front for the hash embedder; Voyage learns it on its first call."""
        return self.inner.dims

    def embed(self, texts) -> list[list[float]]:
        texts = list(texts)
        found: dict[str, tuple[float, ...]] = {}
        with self._lock:
            for text in texts:
                if text in self._memo:
                    self._memo.move_to_end(text)
                    found[text] = self._memo[text]
        missing = [t for t in dict.fromkeys(texts) if t not in found]
        if missing:
            fresh = {t: tuple(v) for t, v in zip(missing, self.inner.embed(missing))}
            found.update(fresh)
            with self._lock:
                self._memo.update(fresh)
                while len(self._memo) > self.maxsize:
                    self._memo.popitem(last=False)
        # Fresh lists, so a caller that edits a vector cannot corrupt the memo.
        return [list(found[t]) for t in texts]


def _identity(embedder) -> tuple:
    """What makes two embedders interchangeable: provider, model, endpoint,
    output dimension and key (Hash has none of the last three)."""
    return (type(embedder).__name__, embedder.name, getattr(embedder, "base_url", None),
            getattr(embedder, "output_dimension", None), getattr(embedder, "api_key", None))


def get_embedder(settings) -> CachedEmbedder:
    """The process-wide embedder for these settings.

    Building a provider client is free (no network), so one is built to
    learn which provider and model the settings pick, and the cached one for
    that identity is returned. Raises SystemExit (make_embedder) when the
    settings ask for Voyage without a key or name an unknown provider.
    """
    fresh = make_embedder(None, env=settings.embed_env())
    key = _identity(fresh)
    with _lock:
        if key not in _embedders:
            _embedders[key] = CachedEmbedder(fresh)
            log.info("memory embedder: %s", fresh.name)
        return _embedders[key]


def embedder_dims(embedder) -> int:
    """The embedder's output size, embedding one short probe if it is not yet
    known (a Voyage client learns it from its first response)."""
    if embedder.dims is not None:
        return embedder.dims
    return len(embedder.embed([_DIMS_PROBE])[0])


def check_embedder(db, embedder) -> tuple[bool, str]:
    """(ok, detail): were the stored episodes embedded by `embedder`'s model,
    at its output size?

    Retrieval only scores episodes embedded by the query's own model (see
    memory.retrieve), so memory from earlier runs embedded with another model
    would be silently invisible. Nothing stored yet is nothing to disagree with.
    """
    stored = db.episodes.find_one({"valid": True, "embedding_model": {"$exists": True}},
                                  {"embedding_model": 1, "embedding": 1}, sort=[("created_at", -1)])
    if stored is None:
        return True, f"no episodes stored yet; episodes are embedded with {embedder.name}"
    model = stored["embedding_model"]
    if model != embedder.name:
        return False, (f"stored episodes were embedded with {model} but the query embedder is {embedder.name}: "
                       "set EMBED_PROVIDER/EMBED_MODEL to match, or reset memory")
    try:
        dims = embedder_dims(embedder)
    except EMBED_ERRORS as exc:
        return False, f"embedder {embedder.name} is unreachable: {exc}"
    have = len(stored.get("embedding") or [])
    if have and dims != have:
        return False, (f"stored vectors have {have} dimensions but {embedder.name} returns {dims}: "
                       "set EMBED_DIMS to match, or reset memory")
    return True, f"query embedder {embedder.name} ({dims} dims) matches the stored episodes"
