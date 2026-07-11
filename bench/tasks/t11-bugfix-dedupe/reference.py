def dedupe_events(events):
    best = {}
    order = []
    for ev in events:
        eid = ev["id"]
        ts = ev["ts"]
        if eid not in best:
            order.append(eid)
            best[eid] = ev
        elif ts > best[eid]["ts"]:
            best[eid] = ev
    return [best[eid] for eid in order]
