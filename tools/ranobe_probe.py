"""Probe the LibGroup API for RanobeLib (site 3): list, chapters, one chapter's content shape."""
import json
import httpx

API = "https://api2.mangalib.me/api"
H = {"Site-Id": "3", "Referer": "https://ranobelib.me/", "Origin": "https://ranobelib.me",
     "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
     "Accept": "application/json"}
c = httpx.Client(headers=H, timeout=30, follow_redirects=True)


def show(label, r):
    print(f"== {label}: {r.status_code} {r.headers.get('content-type')}")
    return r


r = show("catalog", c.get(f"{API}/manga", params={"site_id[]": 3, "page": 1, "sort_by": "rate_avg"}))
items = r.json().get("data") or []
print(json.dumps(items[:2], ensure_ascii=False)[:1500])
for it in items[:3]:
    slug = it.get("slug_url") or it.get("slug")
    ch = show(f"chapters {slug}", c.get(f"{API}/manga/{slug}/chapters")).json().get("data") or []
    print("chapters:", len(ch), json.dumps(ch[:1], ensure_ascii=False)[:600])
    if not ch:
        continue
    first = ch[0]
    params = {"number": first["number"], "volume": first["volume"]}
    br = (first.get("branches") or [{}])[0]
    if br.get("branch_id"):
        params["branch_id"] = br["branch_id"]
    j = show("chapter", c.get(f"{API}/manga/{slug}/chapter", params=params)).json().get("data") or {}
    print("keys:", sorted(j.keys()))
    content = j.get("content")
    print("content type:", type(content).__name__)
    print(json.dumps(content, ensure_ascii=False)[:2500] if not isinstance(content, str) else content[:2500])
    print("attachments:", json.dumps(j.get("attachments"), ensure_ascii=False)[:800])
    if isinstance(content, dict):
        kinds = {}
        def walk(n):
            kinds[n.get("type")] = kinds.get(n.get("type"), 0) + 1
            for k in n.get("content") or []:
                walk(k)
        walk(content)
        print("node types:", kinds)
    # look for one chapter with an image somewhere
    break
# search
r = show("search", c.get(f"{API}/manga", params={"site_id[]": 3, "q": "Поднятие"}))
print(json.dumps([(x.get("rus_name"), x.get("slug_url")) for x in (r.json().get("data") or [])[:5]], ensure_ascii=False))
