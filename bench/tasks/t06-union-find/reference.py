class UnionFind:
    def __init__(self):
        self._parent = {}
        self._rank = {}
        self._count = 0

    def _find(self, x):
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:      # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def _add(self, x):
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
            self._count += 1

    def union(self, a, b):
        self._add(a)
        self._add(b)
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return False
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        self._count -= 1
        return True

    def connected(self, a, b):
        if a == b:
            return True
        if a not in self._parent or b not in self._parent:
            return False
        return self._find(a) == self._find(b)

    def components(self):
        return self._count
