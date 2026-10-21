"""检索层：纯 Python BM25 实现（零依赖）+ Retriever 接口。

为什么用 BM25 而不是向量检索（面试讲点）：
- 知识库只有 7 篇、查询含强区分术语（错误码、服务名、产品名），BM25 命中率足够
- 零依赖、零索引维护、可解释（能说出每篇文章为什么命中）
- 保留 search(query, top_k) 接口形状，将来知识库上千篇、口语化表述占多数时，
  可无缝替换为 embedding 召回 + 重排
"""
import math
import re
from dataclasses import dataclass
from typing import Dict, List

# 英文/数字整词 + 中文连续串
_RUN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """中英混合分词：英文/数字保留整词（小写），中文取字符 bigram。

    中文不引入 jieba 依赖，bigram 对"密码重置"→[密码,码重,重置]这类关键词
    匹配已经足够，且天然支持错误码、产品名等强区分术语。
    """
    tokens: List[str] = []
    for run in _RUN_RE.findall(text.lower()):
        if run.isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i: i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class Hit:
    """一条检索命中。"""

    doc_id: str
    score: float
    text: str  # 原文（供上层提取摘要片段）


class BM25Index:
    """标准 BM25 (Okapi)：IDF 稀有度加权 + 词频饱和 + 文档长度惩罚。"""

    K1 = 1.5
    B = 0.75

    def __init__(self, docs: Dict[str, str]):
        self._raw = dict(docs)
        self._doc_ids = list(docs.keys())
        self._doc_tf: List[Dict[str, int]] = []
        self._doc_len: List[int] = []
        df: Dict[str, int] = {}
        for did in self._doc_ids:
            tokens = tokenize(docs[did])
            tf: Dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            self._doc_tf.append(tf)
            self._doc_len.append(len(tokens))
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self._df = df
        self._n = len(self._doc_ids)
        self._avgdl = (sum(self._doc_len) / self._n) if self._n else 0.0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int = 3) -> List[Hit]:
        if not self._n:
            return []
        q_tokens = tokenize(query)
        scores: List[tuple] = []
        for i in range(self._n):
            dl = self._doc_len[i] or 1
            score = 0.0
            tf_map = self._doc_tf[i]
            for t in q_tokens:
                tf = tf_map.get(t, 0)
                if not tf:
                    continue
                score += self._idf(t) * (tf * (self.K1 + 1)) / (
                    tf + self.K1 * (1 - self.B + self.B * dl / (self._avgdl or 1))
                )
            scores.append((score, i))
        scores.sort(key=lambda x: -x[0])
        return [
            Hit(doc_id=self._doc_ids[i], score=round(s, 4), text=self._raw[self._doc_ids[i]])
            for s, i in scores[:top_k]
            if s > 0
        ]


def best_snippet(text: str, query: str, max_chars: int = 300) -> str:
    """从文档中挑出与查询最相关的段落作为摘要（按段落切分，取命中词最多的段）。"""
    q_tokens = set(tokenize(query))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return text[:max_chars]
    best, best_score = paragraphs[0], -1
    for p in paragraphs:
        score = len(q_tokens & set(tokenize(p)))
        if score > best_score:
            best, best_score = p, score
    return best[:max_chars]
