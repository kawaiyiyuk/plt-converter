"""网络连通分量划分、纯森林悬垂分量剔除与局部场景构面缓存。

仅将整分量完全剥离为空的简单开放森林（孤立文字笔画、浮空箭头、
悬垂树等）排除出构面网；任何含闭环或自交的分量均完整保留，
从而在零几何改动的前提下加速 all_source 与 scenarios 轮廓核对。
"""
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.ops import polygonize_full, unary_union


def _is_forest_component(comp_pids, available, adj):
    """仅当整分量经简单开放叶节点迭代剥离后为空时，才判为无面森林。"""
    active = set(comp_pids)
    changed = True
    while changed and active:
        changed = False
        for pid in list(active):
            line = available[pid]
            if not line.is_simple or line.coords[0] == line.coords[-1]:
                continue
            neighbors = [nbr for nbr in adj[pid] if nbr in active]
            if not neighbors:
                active.remove(pid)
                changed = True
            elif len(neighbors) == 1:
                hit = line.intersection(available[neighbors[0]])
                if hit.is_empty or hit.geom_type == 'Point':
                    active.remove(pid)
                    changed = True
    return len(active) == 0


def _component_dangle_count(comp_pids, available):
    if len(comp_pids) == 1:
        line = available[comp_pids[0]]
        return 1 if line.length > 0 else 0
    net = unary_union([available[pid] for pid in comp_pids])
    _, _, dangles, _ = polygonize_full(net)
    return len(dangles.geoms)


def partition_network(available, path_ids, path_tree, *, count_forest_dangles=True):
    """将 available 划分为森林分量与非森林连通分量。"""
    n = len(path_ids)
    if n == 0:
        return [], set(), 0, []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    geoms = [available[pid] for pid in path_ids]
    pairs = path_tree.query(geoms, predicate='intersects')
    adj = {pid: set() for pid in path_ids}
    if pairs.size:
        for raw_a, raw_b in zip(pairs[0], pairs[1]):
            ia, ib = int(raw_a), int(raw_b)
            if ia == ib:
                continue
            pa, pb = path_ids[ia], path_ids[ib]
            adj[pa].add(pb)
            adj[pb].add(pa)
            ra, rb = find(ia), find(ib)
            if ra != rb:
                parent[rb] = ra

    raw_comps = {}
    for idx, pid in enumerate(path_ids):
        raw_comps.setdefault(find(idx), []).append(pid)

    forest_pids = set()
    non_forest_comps = []
    forest_dangle_count = 0
    for comp_pids in raw_comps.values():
        if _is_forest_component(comp_pids, available, adj):
            forest_pids.update(comp_pids)
            if count_forest_dangles:
                forest_dangle_count += _component_dangle_count(comp_pids, available)
        else:
            non_forest_comps.append(comp_pids)

    non_forest_pids = [pid for pid in path_ids if pid not in forest_pids]
    return non_forest_comps, forest_pids, forest_dangle_count, non_forest_pids


class ScenarioNetworkCache:
    """按受影响的连通分量重算 scenarios 构面并复用未受影响分量。"""

    def __init__(self, available, path_ids, path_tree, non_forest_comps, non_forest_pids, base_faces, base_invalid, eps):
        self.available = available
        self.non_forest_pids = tuple(non_forest_pids)
        self.eps = eps
        self.fallback_full = False
        self.comp_pids = {}
        self.comp_of = {}
        self._scenario_comp_cache = {}

        # 初始化非森林分量编号；后续若检测到跨分量浮点接触则合并。
        comp_parent = {}
        for cid, pids in enumerate(non_forest_comps):
            comp_parent[cid] = cid
            for pid in pids:
                self.comp_of[pid] = cid

        def find_cid(c):
            while comp_parent[c] != c:
                comp_parent[c] = comp_parent[comp_parent[c]]
                c = comp_parent[c]
            return c

        def merge_cids(cids):
            it = iter(cids)
            root = find_cid(next(it))
            for other in it:
                ro = find_cid(other)
                if ro != root:
                    comp_parent[ro] = root
            return root

        def locate_cids(geom):
            hits = path_tree.query(geom, predicate='intersects')
            cids = {self.comp_of[path_ids[int(i)]] for i in hits if path_ids[int(i)] in self.comp_of}
            if not cids and eps > 0:
                hits = path_tree.query(geom.buffer(eps), predicate='intersects')
                cids = {self.comp_of[path_ids[int(i)]] for i in hits if path_ids[int(i)] in self.comp_of}
            return cids

        face_records = []
        invalid_records = []
        try:
            for face in base_faces.geoms:
                cids = locate_cids(face.exterior)
                if not cids:
                    self.fallback_full = True
                    return
                root = merge_cids(cids) if len(cids) > 1 else find_cid(next(iter(cids)))
                face_records.append((root, Polygon(face.exterior)))
            for ring in base_invalid.geoms:
                cids = locate_cids(ring)
                if not cids:
                    self.fallback_full = True
                    return
                root = merge_cids(cids) if len(cids) > 1 else find_cid(next(iter(cids)))
                invalid_records.append(root)
        except GEOSException:
            self.fallback_full = True
            return

        # 归并最终分量映射
        merged_pids = {}
        for pid, old_cid in list(self.comp_of.items()):
            root = find_cid(old_cid)
            self.comp_of[pid] = root
            merged_pids.setdefault(root, []).append(pid)
        self.comp_pids = {cid: tuple(pids) for cid, pids in merged_pids.items()}

        self.base_faces_by_comp = {cid: [] for cid in self.comp_pids}
        for old_cid, poly in face_records:
            self.base_faces_by_comp[find_cid(old_cid)].append(poly)
        self.base_filled_by_comp = {
            cid: unary_union(polys) if polys else None
            for cid, polys in self.base_faces_by_comp.items()
        }
        self.base_invalid_by_comp = {cid: 0 for cid in self.comp_pids}
        for old_cid in invalid_records:
            cid = find_cid(old_cid)
            self.base_invalid_by_comp[cid] += 1

    def scenario_filled_and_invalid(self, excluded):
        if self.fallback_full:
            return self._full_recompute(excluded)
        try:
            affected = {self.comp_of[pid] for pid in excluded if pid in self.comp_of}
            filled_parts = []
            invalid_count = 0
            for cid, pids in self.comp_pids.items():
                if cid not in affected:
                    comp_fill = self.base_filled_by_comp[cid]
                    if comp_fill is not None and not comp_fill.is_empty:
                        filled_parts.append(comp_fill)
                    invalid_count += self.base_invalid_by_comp[cid]
                    continue
                local_excluded = frozenset(pid for pid in pids if pid in excluded)
                cache_key = (cid, local_excluded)
                cached = self._scenario_comp_cache.get(cache_key)
                if cached is None:
                    retained = [self.available[pid] for pid in pids if pid not in local_excluded]
                    if not retained or (len(retained) == 1 and retained[0].is_simple and retained[0].coords[0] != retained[0].coords[-1]):
                        cached = (None, 0)
                    else:
                        sub_net = unary_union(retained)
                        sub_faces, _, _, sub_bad = polygonize_full(sub_net)
                        sub_polys = [Polygon(f.exterior) for f in sub_faces.geoms]
                        comp_fill = unary_union(sub_polys) if sub_polys else None
                        cached = (comp_fill, len(sub_bad.geoms))
                    self._scenario_comp_cache[cache_key] = cached
                comp_fill, sub_invalid = cached
                if comp_fill is not None and not comp_fill.is_empty:
                    filled_parts.append(comp_fill)
                invalid_count += sub_invalid
            filled = unary_union(filled_parts) if filled_parts else Polygon()
            return filled, invalid_count
        except GEOSException:
            return self._full_recompute(excluded)

    def _full_recompute(self, excluded):
        work_lines = [self.available[pid] for pid in self.non_forest_pids if pid not in excluded]
        if not work_lines:
            return Polygon(), 0
        net = unary_union(work_lines)
        pg, _, _, bad = polygonize_full(net)
        filled = unary_union([Polygon(f.exterior) for f in pg.geoms])
        return filled, len(bad.geoms)
