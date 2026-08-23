"""Just-in-time tool discovery (issue #14).

Keyword-based search over the tool catalog so a caller can find the right
tool by intent instead of the full tools/list schema being resident on every
call. Stdlib only -- no embeddings, no vector store. Deterministic: same
query always ranks the same way, no LLM involved in ranking (that would be a
routing decision wearing a search-tool costume, and would forfeit the same
reproducibility this whole project is built around).

Retrieval quality, not the index/search plumbing, is where the real risk is
-- see the CI recall gate in tests/test_tool_discovery.py, which is meant to
catch a regression here before it ships, not just prove the code runs.
"""

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cheap, stdlib-only stemming: strip the handful of suffixes that would
# otherwise split an exact-intent match ("errors" vs "error", "logging" vs
# "log") into two different tokens. Not a real stemmer -- just enough to
# close the gap a keyword index actually hits in practice.
_SUFFIXES = ("ing", "es", "s")


def _stem(word):
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 2 and word.endswith(suf):
            stemmed = word[: -len(suf)]
            # "logging" -> "logg" -> "log": drop a trailing doubled letter
            # left over from stripping "ing" off a doubled-consonant word.
            if len(stemmed) >= 2 and stemmed[-1] == stemmed[-2]:
                stemmed = stemmed[:-1]
            return stemmed
    return word


# Query-side noise words that add false signal to nearly every tool (most
# descriptions contain "for"/"a"/"the" somewhere) without discriminating
# between them. Filtered from queries only -- the index keeps full
# description text, since a stopword appearing in a description isn't the
# problem; a stopword in the *query* diluting the score is.
_STOPWORDS = frozenset("""
a an the is are was were be been being to of for on in at by with from
what which who how do does did show me my get find list all any some this
that these those and or not
""".split())

# A small, general domain-synonym table -- not one entry per failing test
# case, kept to genuinely common alternate phrasings an engineer would
# reach for. Maps a query word to the canonical term(s) actually used in
# tool names/descriptions, expanding (not replacing) the query's tokens.
_SYNONYMS = {
    "machine": ["host"], "machines": ["host"], "vm": ["host"],
    "field": ["column", "resource", "key"], "fields": ["column", "resource", "key"],
    "columns": ["column", "schema"],
    "lint": ["validate"],
    "credential": ["secret"], "credentials": ["secret"],
    "ingest": ["ingestion"],
    "outlier": ["anomaly"], "outliers": ["anomaly"],
    "reconstruct": ["timeline"], "happened": ["timeline"],
    "spare": ["headroom"], "remaining": ["headroom"],
    "config": ["harness", "recommendation"],
    "latest": ["recent"],
    "raw": ["search", "kql"],
}


def tokenize(text, expand_synonyms=False, drop_stopwords=False):
    raw = _TOKEN_RE.findall((text or "").lower())
    if drop_stopwords:
        raw = [w for w in raw if w not in _STOPWORDS]
    words = list(raw)
    if expand_synonyms:
        # Synonym lookup happens on the raw (unstemmed) word -- the table's
        # keys are real words like "machines"; matching after stemming would
        # look up "machin" instead and never hit.
        for w in raw:
            words.extend(_SYNONYMS.get(w, ()))
    return [_stem(w) for w in words]


def build_index(tools):
    """tools: iterable of {"name", "description", ...}. Returns
    (per_tool, doc_freq): per_tool maps each tool name to its
    (name_tokens, desc_tokens); doc_freq maps each token to how many tools'
    name-or-description it appears in, used to down-weight words that are
    common within one cluster of tools (e.g. "discover"/"new"/"service" all
    appear across several discovery-family tools) even though they're not
    generic-English stopwords -- a plain stopword list can't catch
    domain-specific over-common words, but document frequency does."""
    per_tool = {}
    doc_freq = Counter()
    for t in tools:
        name_tokens = set(tokenize(t["name"].replace("_", " ")))
        desc_tokens = set(tokenize(t.get("description", "")))
        per_tool[t["name"]] = (name_tokens, desc_tokens)
        for tok in name_tokens | desc_tokens:
            doc_freq[tok] += 1
    return per_tool, doc_freq


def _idf_weight(token, doc_freq, n_tools):
    # +1 smoothing so a token present in every tool (weight -> near 0) still
    # contributes something rather than dividing by a near-zero log.
    df = doc_freq.get(token, 0)
    return math.log((n_tools + 1) / (df + 1)) + 1


def search(index, query, top_k=5, tool_order=None):
    """Return up to top_k (tool_name, score) pairs, highest score first.
    Ties break by tool_order (the order tools were registered in) so results
    are deterministic rather than dependent on dict iteration order."""
    per_tool, doc_freq = index
    query_tokens = tokenize(query, expand_synonyms=True, drop_stopwords=True)
    if not query_tokens:
        return []
    n_tools = len(per_tool)

    order = {name: i for i, name in enumerate(tool_order)} if tool_order else {}
    scores = Counter()
    for name, (name_tokens, desc_tokens) in per_tool.items():
        score = 0.0
        for qt in query_tokens:
            weight = _idf_weight(qt, doc_freq, n_tools)
            if qt in name_tokens:
                score += 3 * weight
            elif qt in desc_tokens:
                score += weight
        if score:
            scores[name] = score

    ranked = sorted(
        scores.items(),
        key=lambda pair: (-pair[1], order.get(pair[0], 0)),
    )
    return ranked[:top_k]
