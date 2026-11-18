"""Reuse source-path coverage when excluding paths cannot affect a boundary."""
from functools import lru_cache
from math import ulp

from shapely import STRtree, prepare, total_bounds
from shapely.errors import GEOSException
from shapely.ops import unary_union


def _uncovered_length(boundary, cover):
    return 0.0 if cover.covers(boundary) else boundary.difference(cover).length


class ScenarioCoverage:
    def __init__(self, path_covers, eps):
        self.path_covers = path_covers
        self.eps = eps
        path_ids = tuple(path_covers)
        covers = list(path_covers.values())
        full_cover = unary_union(covers)
        prepare(full_cover)
        self.full_cover = full_cover
        self.original_coverage = None

        # A narrow buffer near the precision of large coordinates can collapse
        # differently when a union is evaluated on a subset of the input.
        # Keep at least eight bits of rounding headroom for the shortcut.
        largest_coordinate = max(abs(value) for value in total_bounds(covers))
        if not eps > 256 * ulp(largest_coordinate):
            return
        tree = STRtree(covers)

        @lru_cache(maxsize=256)
        def original_coverage(boundary):
            touching = tuple(path_ids[i] for i in sorted(tree.query(boundary, predicate='intersects')))
            covered = full_cover.covers(boundary)
            gap = 0.0 if covered else boundary.difference(full_cover).length
            return touching, covered, gap

        self.original_coverage = original_coverage

    def for_paths(self, retained, excluded):
        scenario_cover = None

        def uncovered_length(boundary):
            nonlocal scenario_cover
            if not excluded:
                return _uncovered_length(boundary, self.full_cover)
            if scenario_cover is None and self.original_coverage is not None:
                try:
                    touching, covered, gap = self.original_coverage(boundary)
                    if excluded.isdisjoint(touching):
                        # The exclusion removes no buffer on this boundary. Use
                        # the original union for partial gaps near the comparison
                        # threshold, where floating-point differences can matter.
                        if covered or gap > 2 * self.eps:
                            return gap
                    elif unary_union([self.path_covers[pid] for pid in touching
                                      if pid not in excluded]).covers(boundary):
                        return 0.0
                except GEOSException:
                    # A failed shortcut leaves the reference operation in charge.
                    pass
            if scenario_cover is None:
                scenario_cover = unary_union([self.path_covers[pid] for pid in retained])
                prepare(scenario_cover)
            return _uncovered_length(boundary, scenario_cover)

        return uncovered_length
