import math


class RunningStats:
    def __init__(self):
        self.count = 0
        self._mean = 0.0
        self._m2 = 0.0

    def add(self, x):
        self.count += 1
        delta = x - self._mean
        self._mean += delta / self.count
        self._m2 += delta * (x - self._mean)

    def _require(self):
        if self.count == 0:
            raise ValueError("no data")

    @property
    def mean(self):
        self._require()
        return self._mean

    @property
    def variance(self):
        self._require()
        return self._m2 / self.count

    @property
    def stdev(self):
        return math.sqrt(self.variance)

    def merge(self, other):
        out = RunningStats()
        if self.count == 0 and other.count == 0:
            return out
        n = self.count + other.count
        delta = other._mean - self._mean
        out.count = n
        if n:
            out._mean = (self._mean * self.count + other._mean * other.count) / n
            out._m2 = (self._m2 + other._m2 +
                       delta * delta * self.count * other.count / n) if n else 0.0
        return out
