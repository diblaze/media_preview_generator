"""path -> (truth start s, tolerance s, where it came from) for every credits file this lane can judge.

Sources: the harness sets' own truth (in the ct json rows), the 09-27 answer key (items.json verdict + Plex), the
final audit's positional verdicts (pos_verdicts.json: right -> the marker; wrong early/late by off -> marker +/- off),
the 09-28 fresh sample (fresh_verdicts.json with fresh_sample.json for paths), and checks.json frame checks.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SP = HERE.parent


def build() -> dict[str, dict]:
    truth: dict[str, dict] = {}
    items = json.load(open(HERE / "items.json"))
    checks = json.load(open(HERE / "checks.json"))
    for it in items["verdict"] + items["plex"]:
        if it["type"] != "credits" or not it.get("truth"):
            continue
        t = it["truth"]
        if t.get("start") is None:
            continue
        c = checks.get(it["id"])
        lo = c["lo"] if c and "lo" in c else t["start"]
        hi = c["hi"] if c and "hi" in c else t["start"]
        truth.setdefault(it["path"], {"start": t["start"], "lo": lo, "hi": hi, "src": f"items:{it['id']}"})
    pos = json.load(open(SP / "final-audit/work/pos_verdicts.json"))
    for key, v in pos.items():
        path, mtype, start_ms, _ = key.split("|")
        if mtype != "credits" or not start_ms:
            continue
        s = int(start_ms) / 1000
        off = float(v.get("off") or 0)
        if v["v"] == "right":
            t = s
        elif v["v"] == "wrong" and v.get("dir") == "early":
            t = s + off
        elif v["v"] == "wrong" and v.get("dir") == "late":
            t = s - off
        else:
            continue
        truth.setdefault(path, {"start": t, "lo": t, "hi": t, "src": f"pos:{v.get('src')}"})
    fresh = SP / "accuracy-after/work"
    try:
        sample = {
            x.get("fid_key") or x.get("id") or x.get("sid"): x for x in json.load(open(fresh / "fresh_sample.json"))
        }
    except Exception:  # noqa: BLE001
        sample = {}
    for sid, v in json.load(open(fresh / "fresh_verdicts.json")).items():
        x = sample.get(sid)
        if not x or x.get("type") != "credits":
            continue
        s = x["s"] / 1000
        verdict, direction, off = v[0], v[2], float(v[3] or 0)
        if verdict == "right":
            t = s
        elif direction == "early":
            t = s + off
        elif direction == "late":
            t = s - off
        else:
            continue
        truth.setdefault(x["path"], {"start": t, "lo": t, "hi": t, "src": f"fresh:{sid}"})
    return truth


if __name__ == "__main__":
    t = build()
    from collections import Counter

    print(len(t), Counter(v["src"].split(":")[0] for v in t.values()))
    json.dump(t, open(HERE / "truth.json", "w"), indent=0)
