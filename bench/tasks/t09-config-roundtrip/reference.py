import json


def save_config(path, config):
    lines = []
    for key in sorted(config):
        if not key or not isinstance(key, str) or "=" in key or "\n" in key:
            raise ValueError(f"invalid key: {key!r}")
        # json encodes type + escapes newlines/equals exactly
        lines.append(f"{key}={json.dumps(config[key])}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def load_config(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            if not line:
                continue
            key, _, raw = line.partition("=")
            out[key] = json.loads(raw)
    return out
