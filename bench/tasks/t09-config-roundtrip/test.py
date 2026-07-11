import os
import sys
import tempfile

try:
    from solution import save_config, load_config
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

cfg = {
    "name": "oracle",
    "port": 9099,
    "ratio": 0.25,
    "debug": True,
    "quiet": False,
    "note": "a=b with = signs and  spaces",
    "empty": "",
    "nothing": None,
    "tricky": "line1\nline2",
    "numstr": "42",
}

with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "c.conf")
    save_config(p, cfg)
    out = load_config(p)
    assert out == cfg, out
    for k in cfg:
        assert type(out[k]) is type(cfg[k]), (k, type(out[k]), type(cfg[k]))

    # keys sorted in the file
    with open(p, encoding="utf-8") as f:
        keys = [ln.split("=", 1)[0] for ln in f.read().splitlines() if ln]
    assert keys == sorted(keys), keys

    # invalid keys
    for bad in ["", "a=b", "x\ny"]:
        try:
            save_config(p, {bad: 1})
            raise SystemExit(f"expected ValueError for key {bad!r}")
        except ValueError:
            pass

    try:
        load_config(os.path.join(td, "missing.conf"))
        raise SystemExit("expected FileNotFoundError")
    except FileNotFoundError:
        pass

print("ok")
sys.exit(0)
