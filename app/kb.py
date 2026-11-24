from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import ROOT, settings

TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u0900-\u097F]+", re.UNICODE)
CITE_RE = re.compile(r"$$(KB-\d{2})$$", re.I)


@dataclass
class Article:
    id: str
    title: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


def _kb_dir() -> Path:
    path = settings.data_dir / "kb"
    path.mkdir(parents=True, exist_ok=True)
    bundled = ROOT / "data" / "kb"
    if bundled.exists() and not any(path.glob("KB-*.md")):
        for src in bundled.glob("KB-*.md"):
            dest = path / src.name
            if not dest.exists():
                dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return path if any(path.glob("KB-*.md")) else bundled


def load_articles() -> list[Article]:
    folder = _kb_dir()
    articles: list[Article] = []
    if not folder.exists():
        return articles
    for path in sorted(folder.glob("KB-*.md")):
        raw = path.read_text(encoding="utf-8")
        lines = raw.strip().splitlines()
        title = lines[0].lstrip("# ").strip() if lines else path.stem
        body = "\n".join(lines[1:]).strip()
        articles.append(Article(id=path.stem.upper(), title=title, body=body))
    return articles


def _tokens(text: str) -> list[str]:
    words = [t.lower() for t in TOKEN_RE.findall(text or "")]
    compact = re.sub(r"\s+", "", text or "").lower()
    grams = [compact[i : i + 3] for i in range(max(0, len(compact) - 2))]
    return words + grams


def _tf(tokens: list[str]) -> dict[str, float]:
    counts: dict[str, float] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0.0) + 1.0
    n = float(len(tokens) or 1)
    return {k: v / n for k, v in counts.items()}


class Retriever:
    def __init__(self) -> None:
        self.articles = load_articles()
        self._doc_tf = [_tf(_tokens(a.text)) for a in self.articles]
        df: dict[str, int] = {}
        for tf in self._doc_tf:
            for term in tf:
                df[term] = df.get(term, 0) + 1
        n = max(1, len(self.articles))
        self._idf = {t: math.log((n + 1) / (df_t + 1)) + 1.0 for t, df_t in df.items()}

    def _vec(self, tf: dict[str, float]) -> dict[str, float]:
        return {t: tfv * self._idf.get(t, 1.0) for t, tfv in tf.items()}

    @staticmethod
    def _cos(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(a[t] * b[t] for t in a if t in b)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return float(dot / ((na * nb) + 1e-9))

    def search(self, query: str, k: int = 3, min_score: float = 0.04) -> list[dict[str, Any]]:
        qv = self._vec(_tf(_tokens(query)))
        scored = []
        for art, tf in zip(self.articles, self._doc_tf):
            score = self._cos(qv, self._vec(tf))
            scored.append((score, art))
        scored.sort(key=lambda x: x[0], reverse=True)
        hits = []
        for score, art in scored[:k]:
            if score < min_score:
                continue
            hits.append(
                {
                    "id": art.id,
                    "title": art.title,
                    "snippet": art.body[:500],
                    "score": round(float(score), 4),
                }
            )
        return hits


_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever


def search_kb(query: str, k: int = 3) -> list[dict[str, Any]]:
    return get_retriever().search(query, k=k)


def citations_in(text: str) -> list[str]:
    return [m.upper() for m in CITE_RE.findall(text or "")]


def retrieval_precision(hits: list[dict[str, Any]], relevant_ids: list[str]) -> dict[str, Any]:
    rel = {r.upper() for r in relevant_ids}
    retrieved = [h["id"].upper() for h in hits]
    if not retrieved:
        return {"precision": 1.0 if not rel else 0.0, "retrieved": [], "relevant": sorted(rel), "relevant_retrieved": []}
    relevant_retrieved = [i for i in retrieved if i in rel]
    return {
        "precision": len(relevant_retrieved) / len(retrieved),
        "retrieved": retrieved,
        "relevant": sorted(rel),
        "relevant_retrieved": relevant_retrieved,
    }


def groundedness(answer: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    text = (answer or "").strip()
    low = text.lower()
    refusal = any(
        p in low
        for p in [
            "पुष्टि नहीं",
            "जानकारी नहीं",
            "not available",
            "do not have",
            "don't have",
            "cannot share",
            "नहीं बता",
            "ज्ञान आधार में नहीं",
        ]
    )
    if not hits:
        return {"score": 1.0 if refusal or not text else 0.0, "refusal": refusal, "supported": 0, "claims": 0}

    blob = " ".join(h["snippet"] for h in hits).lower()
    hit_tokens = set(_tokens(blob))
    sentences = [s.strip() for s in re.split(r"[।.!?]\s+", text) if len(s.strip()) > 8]
    if not sentences:
        sentences = [text]
    supported = 0
    for sent in sentences:
        overlap = len(set(_tokens(sent)) & hit_tokens)
        if overlap >= 3 or CITE_RE.search(sent):
            supported += 1
    score = supported / max(1, len(sentences))
    if refusal and score < 0.5:
        score = 0.8
    return {"score": round(score, 3), "refusal": refusal, "supported": supported, "claims": len(sentences)}


def citation_support(answer: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    cited = citations_in(answer)
    retrieved = {h["id"].upper() for h in hits}
    if not cited:
        ok = not hits or groundedness(answer, hits).get("refusal")
        return {"score": 1.0 if ok else 0.0, "cited": [], "supported": [], "unsupported": []}
    supported = [c for c in cited if c in retrieved]
    unsupported = [c for c in cited if c not in retrieved]
    return {
        "score": len(supported) / len(cited),
        "cited": cited,
        "supported": supported,
        "unsupported": unsupported,
    }