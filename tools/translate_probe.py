"""Which free translation endpoints answer from GitHub Actions? (run by translate-probe.yml)

For each engine: 25 quick single-title requests, then one ~4500-character request.
Prints ok/fail counts, the first error, and a sample translation.
"""
import json
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
TITLES = [
    "Pope Leo calls for universal healthcare weeks after lung cancer surgery",
    "Ousted Paraguayan president appears before prosecutors",
    "New Zealand PM sees path back to office but vote too close to call",
    "Israeli president picks Netanyahu's rival Eisenkot to try to form government",
    "Malaysian king warns against defying house arrest order for ex-PM Najib",
] * 5
LONG = ("The central bank held interest rates steady on Thursday, saying inflation was cooling but "
        "remained above its target, while economists expect cuts to begin early next year. ") * 25


def gtx(texts):
    out = []
    for t in texts:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": "th", "dt": "t", "q": t}, headers=UA, timeout=20)
        r.raise_for_status()
        out.append("".join(seg[0] for seg in r.json()[0] if seg and seg[0]))
    return out


def dict_ext(texts):
    r = requests.post("https://clients5.google.com/translate_a/t",
                      params={"client": "dict-chrome-ex", "sl": "auto", "tl": "th"},
                      data={"q": texts}, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()
    return [x[0] if isinstance(x, list) else x for x in res]


_PA_KEY = None


def translate_pa(texts):
    global _PA_KEY
    if _PA_KEY is None:
        js = requests.get("https://translate.googleapis.com/translate_a/element.js", headers=UA, timeout=20).text
        m = re.search(r"AIza[0-9A-Za-z_-]{35}", js)
        _PA_KEY = m.group(0) if m else ""
    if not _PA_KEY:
        raise RuntimeError("no key in element.js")
    r = requests.post("https://translate-pa.googleapis.com/v1/translateHtml",
                      headers={**UA, "Content-Type": "application/json+protobuf", "X-Goog-API-Key": _PA_KEY},
                      data=json.dumps([[texts, "auto", "th"], "te"]), timeout=20)
    r.raise_for_status()
    return r.json()[0]


_EDGE = {"tok": None}


def edge(texts):
    if not _EDGE["tok"]:
        r = requests.get("https://edge.microsoft.com/translate/auth", headers=UA, timeout=20)
        r.raise_for_status()
        _EDGE["tok"] = r.text
    r = requests.post("https://api-edge.cognitive.microsofttranslator.com/translate",
                      params={"api-version": "3.0", "to": "th"},
                      headers={**UA, "Authorization": "Bearer " + _EDGE["tok"], "Content-Type": "application/json"},
                      json=[{"Text": t} for t in texts], timeout=20)
    r.raise_for_status()
    return [x["translations"][0]["text"] for x in r.json()]


def probe(name, fn):
    ok = fail = 0
    first_err = None
    sample = None
    t0 = time.monotonic()
    for t in TITLES:
        try:
            res = fn([t])
            ok += 1
            sample = sample or res[0]
        except Exception as e:  # noqa: BLE001
            fail += 1
            first_err = first_err or f"{type(e).__name__}: {str(e)[:160]}"
    try:
        big = fn([LONG])
        big_res = f"ok ({len(LONG)} chars -> {len(big[0])})"
    except Exception as e:  # noqa: BLE001
        big_res = f"FAIL {type(e).__name__}: {str(e)[:160]}"
    try:
        batch = fn(TITLES[:5])
        batch_res = f"ok ({len(batch)} items)"
    except Exception as e:  # noqa: BLE001
        batch_res = f"FAIL {type(e).__name__}: {str(e)[:160]}"
    print(f"=== {name}: {ok}/{ok + fail} ok in {time.monotonic() - t0:.1f}s | long: {big_res} | batch: {batch_res}")
    print(f"    first error: {first_err}")
    print(f"    sample: {sample}")


if __name__ == "__main__":
    for name, fn in [("gtx", gtx), ("dict-chrome-ex", dict_ext), ("translate-pa (widget)", translate_pa), ("edge (Microsoft)", edge)]:
        probe(name, fn)
