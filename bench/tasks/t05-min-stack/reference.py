class MinStack:
    def __init__(self):
        self._items = []
        self._mins = []

    def push(self, x):
        self._items.append(x)
        if not self._mins or x <= self._mins[-1]:
            self._mins.append(x)

    def pop(self):
        if not self._items:
            raise IndexError("pop from empty stack")
        x = self._items.pop()
        if self._mins and x == self._mins[-1]:
            self._mins.pop()
        return x

    def peek(self):
        if not self._items:
            raise IndexError("peek on empty stack")
        return self._items[-1]

    def minimum(self):
        if not self._mins:
            raise IndexError("minimum on empty stack")
        return self._mins[-1]

    def __len__(self):
        return len(self._items)
