"""Chunker bounds/overlap and ranker sanity (BM25, embeddings, hybrid)."""
from __future__ import annotations

from tools.chunker import approx_tokens, chunk_text, split_sentences
from tools.ranker import BM25, HashEmbedder, cosine, hybrid_rank, tokenize


def test_split_sentences():
    s = split_sentences('First one. Second here! "Third?" Yes.')
    assert len(s) >= 3


def test_chunk_sizes_and_coverage():
    text = " ".join(f"Sentence number {i} talks about topic {i % 7} in detail." for i in range(200))
    chunks = chunk_text(text, target_tokens=100, overlap_tokens=20)
    assert len(chunks) > 3
    for c in chunks[:-1]:
        assert c.tokens <= 130  # hard cap ~1.25x target
    joined = " ".join(c.text for c in chunks)
    assert "Sentence number 0" in joined and "Sentence number 199" in joined


def test_chunk_overlap_present():
    text = " ".join(f"Alpha beta gamma delta sentence {i}." for i in range(100))
    chunks = chunk_text(text, target_tokens=60, overlap_tokens=12)
    assert len(chunks) >= 2
    # last sentence of chunk 0 reappears at the start of chunk 1
    tail = chunks[0].text.split(".")[-2].strip()
    assert tail and tail in chunks[1].text


def test_empty_text_gives_no_chunks():
    assert chunk_text("") == []


def test_bm25_ranks_relevant_doc_first():
    docs = [
        "the annual migration of arctic terns covers enormous distances",
        "sourdough bread needs a mature starter and patient folding",
        "arctic terns breed in the far north and winter near antarctica",
    ]
    bm = BM25([tokenize(d) for d in docs])
    scores = bm.scores("arctic tern migration")
    assert scores[0] > scores[1]
    assert scores[2] > scores[1]


def test_hash_embedder_deterministic_and_normalized():
    e = HashEmbedder(dim=64)
    v1, v2 = e.embed(["glass frogs on leaves", "glass frogs on leaves"])
    assert v1 == v2
    assert abs(cosine(v1, v2) - 1.0) < 1e-9
    far = e.embed(["quarterly bond yields"])[0]
    assert cosine(v1, far) < 0.9


def test_hybrid_rank_prefers_on_topic_chunk():
    texts = [
        "transit budget approved with forty new electric buses for the city",
        "a recipe for braised leeks with brown butter and thyme",
        "the council transit plan extends the light rail to the university",
    ]
    ranked = hybrid_rank("city transit budget light rail", texts)
    assert ranked[0].index in (0, 2)
    assert ranked[-1].index == 1


def test_approx_tokens():
    assert approx_tokens("one two three") == 3
