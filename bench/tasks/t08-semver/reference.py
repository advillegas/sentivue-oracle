def _parse(v):
    if not v or not isinstance(v, str):
        raise ValueError("empty version")
    core, _, pre = v.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError(f"bad core: {v}")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError(f"non-numeric core: {v}")
    pre_ids = pre.split(".") if pre else []
    return nums, pre_ids


def _cmp_pre(a, b):
    if not a and not b:
        return 0
    if not a:
        return 1          # release > prerelease
    if not b:
        return -1
    for x, y in zip(a, b):
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            if int(x) != int(y):
                return -1 if int(x) < int(y) else 1
        elif xn:
            return -1     # numeric < alphanumeric
        elif yn:
            return 1
        elif x != y:
            return -1 if x < y else 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0


def compare(a, b):
    (ca, pa), (cb, pb) = _parse(a), _parse(b)
    if ca != cb:
        return -1 if ca < cb else 1
    return _cmp_pre(pa, pb)
