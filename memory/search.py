from __future__ import annotations

try:
    from rank_bm25 import BM25Okapi
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False

import re


def _tokenize(text: str) -> list[str]:
    return re.findall(r'\b[а-яёa-zA-Z0-9]{2,}\b', text.lower())


def search_in(query: str, items: list[dict], field: str, top_k: int = 5) -> list[dict]:
    if not items:
        return []
    corpus = [_tokenize(item.get(field, "")) for item in items]
    q_tokens = _tokenize(query)
    if not q_tokens:
        return items[:top_k]
    if _HAS_BM25:
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(q_tokens)
    else:
        scores = [
            sum(1 for t in q_tokens if t in doc)
            for doc in corpus
        ]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for score, item in ranked if score > 0][:top_k]
