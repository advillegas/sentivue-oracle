import re

LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (DEBUG|INFO|WARN|ERROR) (\S+): (.*)$")


def parse_logs(text):
    counts = {}
    errors = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            malformed += 1
            continue
        level, component, message = m.groups()
        counts[level] = counts.get(level, 0) + 1
        if level == "ERROR":
            errors.append((component, message))
    return {"counts": counts, "errors": errors, "malformed": malformed}
