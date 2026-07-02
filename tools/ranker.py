"""Hybrid ranking: in-house BM25 + cosine similarity over embeddings.

Embeddings come from a local OpenAI-compatible endpoint when configured;
otherwise a deterministic feature-hashing vectorizer is used so the pipeline
never requires an external API. Hybrid score = mean of the two normalized
signals.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

import httpx

_TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    """a an and are as at be but by for from has have if in into is it its of on
    or that the their there these this to was were what when where which who
    will with you your not no""".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


class BM25:
    """Okapi BM25, implemented in-house (k1=1.5, b=0.75)."""

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = corpus_tokens
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if corpus_tokens else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in corpus_tokens:
            counts: dict[str, int] = {}
            for t in doc:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                df[t] = df.get(t, 0) + 1
        n = len(corpus_tokens)
        self.idf = {
            t: math.log((n - d + 0.5) / (d + 0.5) + 1.0) for t, d in df.items()
        }

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        if not self.doc_tokens:
            return 0.0
        counts = self.tf[doc_index]
        dl = self.doc_len[doc_index] or 1
        score = 0.0
        for t in query_tokens:
            if t not in counts:
                continue
            f = counts[t]
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
            score += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
        return score

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        return [self.score(q, i) for i in range(len(self.doc_tokens))]


class HashEmbedder:
    """Deterministic feature-hashing vectorizer (no external calls)."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _slot(self, token: str) -> tuple[int, int]:
        h = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % self.dim
        sign = 1 if h[4] & 1 else -1
        return idx, sign

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in tokenize(text):
                idx, sign = self._slot(tok)
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class LocalEmbedder:
    """Embeddings via a local OpenAI-compatible /embeddings endpoint."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._http.post(
            f"{self.base_url}/embeddings", json={"model": self.model, "input": texts}
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (na * nb)


@dataclass
class RankedItem:
    index: int
    score: float
    bm25: float
    cos: float


def hybrid_rank(query: str, texts: list[str], embedder=None) -> list[RankedItem]:
    """Rank `texts` against `query`; returns items sorted best-first."""
    if not texts:
        return []
    embedder = embedder or HashEmbedder()
    bm25 = BM25([tokenize(t) for t in texts])
    raw_bm = bm25.scores(query)
    max_bm = max(raw_bm) or 1.0
    vecs = embedder.embed([query] + texts)
    qv, tvs = vecs[0], vecs[1:]
    items = []
    for i, text in enumerate(texts):
        c = cosine(qv, tvs[i])
        c01 = (c + 1.0) / 2.0  # cosine in [-1,1] → [0,1]
        b01 = raw_bm[i] / max_bm
        items.append(RankedItem(i, 0.5 * b01 + 0.5 * c01, raw_bm[i], c))
    items.sort(key=lambda r: r.score, reverse=True)
    return items


def make_embedder(embeddings_cfg) -> object:
    if embeddings_cfg.provider == "local" and embeddings_cfg.base_url:
        return LocalEmbedder(embeddings_cfg.base_url, embeddings_cfg.model)
    return HashEmbedder()
