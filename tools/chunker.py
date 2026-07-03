"""Sentence/paragraph chunker.

Splits extracted text into chunks of roughly `target_tokens` (whitespace words
are used as the token approximation), never exceeding ~25% over target, with a
small sentence-level overlap between adjacent chunks so no claim is cut in
half at a boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def approx_tokens(text: str) -> int:
    return len(text.split())


def split_sentences(paragraph: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(paragraph) if p.strip()]
    return parts or ([paragraph.strip()] if paragraph.strip() else [])


@dataclass
class Chunk:
    text: str
    index: int
    tokens: int


def chunk_text(
    text: str, target_tokens: int = 400, overlap_tokens: int = 50
) -> list[Chunk]:
    """Pack sentences into ~target_tokens chunks with a small overlap."""
    hard_cap = int(target_tokens * 1.25)
    sentences: list[str] = []
    for para in re.split(r"\n{2,}|\n", text):
        sentences.extend(split_sentences(para))
    if not sentences:
        return []

    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_tokens = 0
    for sent in sentences:
        st = approx_tokens(sent)
        if cur and cur_tokens + st > hard_cap:
            chunks.append(Chunk(" ".join(cur), len(chunks), cur_tokens))
            # seed the next chunk with trailing sentences as overlap
            overlap: list[str] = []
            otok = 0
            for prev in reversed(cur):
                ptok = approx_tokens(prev)
                if otok + ptok > overlap_tokens:
                    break
                overlap.insert(0, prev)
                otok += ptok
            cur = overlap
            cur_tokens = otok
        cur.append(sent)
        cur_tokens += st
        if cur_tokens >= target_tokens:
            chunks.append(Chunk(" ".join(cur), len(chunks), cur_tokens))
            overlap = []
            otok = 0
            for prev in reversed(cur):
                ptok = approx_tokens(prev)
                if otok + ptok > overlap_tokens:
                    break
                overlap.insert(0, prev)
                otok += ptok
            cur = overlap
            cur_tokens = otok
    if cur and (not chunks or " ".join(cur) != chunks[-1].text):
        tail = " ".join(cur)
        # drop a tail that is pure overlap of the previous chunk
        if not chunks or tail not in chunks[-1].text:
            chunks.append(Chunk(tail, len(chunks), cur_tokens))
    return chunks
