import math


def outliers(csv_text, k=2.0):
    rows = []
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    for i, line in enumerate(lines[1:], start=1):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"malformed row at data line {i}")
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            raise ValueError(f"non-numeric value at data line {i}")
    if len(rows) < 2:
        return []
    vals = [v for _, v in rows]
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    if sd == 0:
        return []
    return [n for n, v in rows if abs(v - mean) > k * sd]
