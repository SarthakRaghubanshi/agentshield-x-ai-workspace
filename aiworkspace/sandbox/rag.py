"""Small retrieval component over the synthetic knowledge base (PRD FR-15).

TF-IDF vectors in a FAISS inner-product index: fully offline, deterministic, no model download.
Falls back to a NumPy search if faiss is not installed.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

try:
    import faiss
except ImportError:  # pragma: no cover
    faiss = None


class KnowledgeBase:
    def __init__(self, kb_dir: Path):
        self.kb_dir = Path(kb_dir)
        self.chunks: list[dict] = []
        self.vectorizer: TfidfVectorizer | None = None
        self.index = None
        self.matrix: np.ndarray | None = None
        self.build()

    def build(self) -> None:
        self.chunks = []
        for path in sorted(self.kb_dir.glob("**/*")):
            if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
                self.chunks.append({"doc": path.relative_to(self.kb_dir).as_posix(), "text": para})
        if not self.chunks:
            return
        # Each chunk is prefixed with its document title so short paragraphs keep context.
        corpus = [f"{c['doc']} {c['text']}" for c in self.chunks]
        self.vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
        matrix = self.vectorizer.fit_transform(corpus).toarray().astype("float32")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, 1e-9)
        self.matrix = matrix
        if faiss is not None:
            self.index = faiss.IndexFlatIP(matrix.shape[1])
            self.index.add(matrix)

    def search(self, query: str, k: int = 3) -> list[dict]:
        if not self.chunks or self.vectorizer is None:
            return []
        q = self.vectorizer.transform([query]).toarray().astype("float32")
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        k = max(1, min(k, len(self.chunks)))
        if self.index is not None:
            scores, ids = self.index.search(q, k)
            pairs = zip(scores[0].tolist(), ids[0].tolist())
        else:
            sims = (self.matrix @ q[0]).tolist()
            order = sorted(range(len(sims)), key=lambda i: -sims[i])[:k]
            pairs = [(sims[i], i) for i in order]
        return [self.chunks[i] | {"score": float(s)} for s, i in pairs if i >= 0 and s > 0]
