"""Rank fusion.

Keyword and vector scores mean different things, so they are combined by *rank*, not by
arithmetic on raw scores. RRF adds 1/(c+rank) for each appearance across ranked lists.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

RRF_C = 60


def reciprocal_rank_fusion(rankings: Iterable[Sequence[str]], c: int = RRF_C) -> list[str]:
    """Fuse ranked ID lists. Ties break on ID so the order is deterministic."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, record_id in enumerate(ranking, start=1):
            if record_id in seen:
                continue
            seen.add(record_id)
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (c + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))


def cap_per_source(
    ordered_ids: Sequence[str], source_of: dict[str, str], max_per_source: int
) -> list[str]:
    """Stop one verbose table's chunks from crowding out required objects."""
    counts: dict[str, int] = {}
    kept: list[str] = []
    for record_id in ordered_ids:
        source = source_of.get(record_id, record_id)
        if counts.get(source, 0) >= max_per_source:
            continue
        counts[source] = counts.get(source, 0) + 1
        kept.append(record_id)
    return kept
