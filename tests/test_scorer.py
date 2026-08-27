"""Tests for context scoring.

`query_text` was accepted and ignored, so every node scored the same
regardless of what the user asked. These pin the lexical fallback that made
the parameter live.
"""

import datetime
import json

from contextmesh.config import RouterConfig
from contextmesh.models.nodes import NodeType
from contextmesh.router.scorer import ContextScorer

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()


def _node(node_type, content="", files=()):
    return {
        "node_id": content[:8] or node_type,
        "node_type": node_type,
        "created_at": NOW,
        "files_involved": json.dumps(list(files)),
        "content": content,
    }


def scorer():
    return ContextScorer(RouterConfig())


def test_lexical_overlap_rewards_matching_words():
    s = scorer()
    assert s.lexical_score("migrate billing to stripe", "billing migration via stripe") > 0.5


def test_lexical_overlap_ignores_stopwords():
    """Sharing only filler words is not a match."""
    assert scorer().lexical_score("what is the file", "the file is there") == 0.0


def test_stemming_matches_word_variants():
    s = scorer()
    assert s.lexical_score("continue the migration", "migrate the module") > 0.0


def test_empty_text_scores_zero():
    s = scorer()
    assert s.lexical_score("", "anything") == 0.0
    assert s.lexical_score("anything", "") == 0.0


def test_query_text_now_changes_the_score():
    """The regression that made relevance gating impossible."""
    s = scorer()
    node = [_node(NodeType.DECISION.value, "we chose postgres over mysql for billing")]

    related = s.score(node, [], None, {}, "why did we pick postgres for billing")[0][1]
    unrelated = s.score(node, [], None, {}, "what colour is the logo")[0][1]

    assert related > unrelated


def test_file_overlap_still_contributes():
    s = scorer()
    node = [_node(NodeType.DECISION.value, "irrelevant text", ["settings.py"])]
    with_match = s.score(node, ["settings.py"], None, {}, "")[0][1]
    without = s.score(node, ["other.py"], None, {}, "")[0][1]
    assert with_match > without


def test_unresolved_issues_outrank_plain_events():
    s = scorer()
    pair = [_node(NodeType.UNRESOLVED_ISSUE.value, "x"), _node(NodeType.FILE_READ.value, "x")]
    ranked = s.score(pair, [], None, {}, "")
    assert ranked[0][0]["node_type"] == NodeType.UNRESOLVED_ISSUE.value
