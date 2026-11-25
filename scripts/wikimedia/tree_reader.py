# Created by d.kotina on 24.11.2026
"""Потоковое проверяемое чтение immutable дерева без загрузки всего набора в память."""

from scripts.wikimedia.blocks import decode_document_block
from scripts.wikimedia.bundle import validate_directory_row
from scripts.wikimedia.common import require

# MARK: - Verified Iteration

def iter_verified_tree(get_block, descriptor, validator, identity_field="qid", maximum_rows=10_000_000):
    """Проверяет SHA/диапазоны/уникальность и выдаёт строки в порядке ключей под явным лимитом."""
    require(isinstance(descriptor, dict) and type(descriptor.get("count")) is int
            and 0 <= descriptor["count"] <= maximum_rows, "invalid_streamed_tree_count")
    if descriptor["count"] == 0:
        require(descriptor == {"count": 0, "depth": 0}, "invalid_empty_streamed_tree")
        return
    require(type(descriptor.get("depth")) is int and 0 <= descriptor["depth"] <= 8,
            "invalid_streamed_tree_depth")
    pending = [(descriptor["root"], descriptor["depth"], descriptor["first"], descriptor["last"])]
    visited, previous, count = set(), None, 0
    while pending:
        digest, depth, first, last = pending.pop()
        require(digest not in visited and len(visited) < 500_000, "streamed_tree_repeated_or_excess_blocks")
        visited.add(digest)
        rows = decode_document_block(digest, get_block(digest), validate_directory_row if depth else validator,
                                     "key" if depth else identity_field)
        keys = sorted(rows)
        require(bool(keys) and keys[-1] == last
                and (rows[keys[0]]["first"] if depth else keys[0]) == first, "streamed_tree_range_mismatch")
        if depth:
            children, boundary = [], None
            for key in keys:
                row = rows[key]
                require(boundary is None or boundary < row["first"], "streamed_tree_overlapping_ranges")
                children.append((row["block"], depth - 1, row["first"], row["key"]))
                boundary = row["key"]
            pending.extend(reversed(children))
        else:
            for key in keys:
                require(previous is None or previous < key, "streamed_tree_unsorted_or_duplicate")
                count += 1
                require(count <= descriptor["count"], "streamed_tree_excess_rows")
                previous = key
                yield key, rows[key]
    require(count == descriptor["count"], "streamed_tree_missing_rows")
