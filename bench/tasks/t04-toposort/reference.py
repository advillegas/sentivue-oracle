import heapq


def toposort(edges, nodes=None):
    adj = {}
    indeg = {}

    def ensure(n):
        adj.setdefault(n, [])
        indeg.setdefault(n, 0)

    for before, after in edges:
        ensure(before)
        ensure(after)
        adj[before].append(after)
        indeg[after] += 1
    for n in nodes or []:
        ensure(n)

    ready = [n for n, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        n = heapq.heappop(ready)
        out.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(ready, m)
    if len(out) != len(indeg):
        stuck = sorted(n for n, d in indeg.items() if d > 0)
        raise ValueError(f"cycle involving: {', '.join(stuck)}")
    return out
