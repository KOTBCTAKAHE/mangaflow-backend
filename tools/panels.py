"""Reference implementation of the app's panel detector (mirror of reader_panels.dart)."""
import numpy as np
from scipy import ndimage


def paper(g):
    h, w = g.shape
    border = np.sort(np.concatenate([g[0], g[1], g[h - 2], g[h - 1], g[:, 0], g[:, 1], g[:, w - 2], g[:, w - 1]]))
    pct = lambda v, q: int(v[int(round((len(v) - 1) * q))])
    near = lambda t: float((np.abs(border.astype(int) - t) <= 16).mean())
    hi, lo = pct(border, 0.9), pct(border, 0.1)
    if near(hi) >= 0.25:
        return hi, False
    if near(lo) >= 0.4 and lo < 80:
        return lo, True
    allv = np.sort(g.ravel()[::7])
    return pct(allv, 0.97), False


def detect(g, rtl=True, params=None):
    """g: uint8 h×w gray. Returns list of (x0,y0,x1,y1) in reading order (pixels)."""
    P = dict(tol=24, min_area=0.012, gut=0.02, gut_out=0.85, min_part_ink=0.08, gutter="outside", line_hi=0.9, line_side=0.8, line_min_side=0.6, line_max=10, contain=0.6)
    P.update(params or {})
    h, w = g.shape
    if h > w * 3:
        return [(0, 0, w, h)]
    bg, dark = paper(g)
    gi = g.astype(int)
    ink = (gi > bg + P["tol"]) if dark else (gi < bg - P["tol"])
    bgm = ~ink
    blab, _ = ndimage.label(bgm)  # 4-connected: thin borders stop it
    edge = np.unique(np.concatenate([blab[0], blab[-1], blab[:, 0], blab[:, -1]]))
    outside = np.isin(blab, edge[edge > 0])
    lab, n = ndimage.label(ink, structure=np.ones((3, 3)))
    boxes = []
    for s in ndimage.find_objects(lab):
        boxes.append((s[1].start, s[0].start, s[1].stop, s[0].stop))
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
    def inter(a, b):
        iw = min(a[2], b[2]) - max(a[0], b[0]); ih = min(a[3], b[3]) - max(a[1], b[1])
        return iw * ih if iw > 0 and ih > 0 else 0
    min_area = w * h * P["min_area"]
    big = sorted([b for b in boxes if area(b) >= min_area], key=area, reverse=True)
    blocks = []
    for b in big:
        if any(inter(b, k) >= area(b) * P["contain"] for k in blocks):
            continue
        blocks.append(b)
    if not blocks:
        return [(0, 0, w, h)]

    def trim(r):
        x0, y0, x1, y1 = r
        sub = ink[y0:y1, x0:x1]
        rows = sub.sum(1) > (x1 - x0) * 0.01
        cols = sub.sum(0) > (y1 - y0) * 0.01
        if not rows.any() or not cols.any():
            return None
        ry = np.where(rows)[0]; cx = np.where(cols)[0]
        r2 = (x0 + cx[0], y0 + ry[0], x0 + cx[-1] + 1, y0 + ry[-1] + 1)
        if r2[2] - r2[0] < 6 or r2[3] - r2[1] < 6:
            return None
        return r2

    def split(r, rows):
        x0, y0, x1, y1 = r
        sub = ink[y0:y1, x0:x1]
        out_ = outside[y0:y1, x0:x1]
        d = sub.mean(1) if rows else sub.mean(0)
        o = out_.mean(1) if rows else out_.mean(0)
        n = len(d)
        # a gutter is white that reaches the page edge: a bubble may cross it, a panel's sky is not one
        sep = (d <= P["gut"]) if P["gutter"] == "ink" else (o >= P["gut_out"])
        line = np.zeros(n, bool)
        def mean(a, b):
            a = max(0, min(n, a)); b = max(0, min(n, b))
            return 1.0 if b <= a else float(d[a:b].mean())
        i = 0
        while i < n:
            if d[i] < P["line_hi"]:
                i += 1; continue
            j = i
            while True:
                while j < n and d[j] >= P["line_hi"]:
                    j += 1
                k = j
                while k < n and k - j < 3 and d[k] < P["line_hi"]:
                    k += 1
                if k < n and k > j and d[k] >= P["line_hi"]:
                    j = k; continue
                break
            if j - i <= P["line_max"] and i >= 3 and j + 3 <= n:
                before = mean(i - 6, i - 2); after = mean(j + 2, j + 6)
                if before <= P["line_side"] and after <= P["line_side"] and min(before, after) <= P["line_min_side"]:
                    line[i:j] = True; sep[i:j] = True
            i = j
        parts = []; start = 0; i = 0
        while i < n:
            if not sep[i]:
                i += 1; continue
            j = i
            while j < n and sep[j]:
                j += 1
            if (j - i >= 2 or line[i]) and i > start:
                parts.append((x0, y0 + start, x1, y0 + i) if rows else (x0 + start, y0, x0 + i, y1)); start = j
            i = j
        if start < n:
            parts.append((x0, y0 + start, x1, y1) if rows else (x0 + start, y0, x1, y1))
        return [p for p in parts if ((p[3] - p[1]) if rows else (p[2] - p[0])) >= 6]

    def cut(region, depth, out):
        r = trim(region)
        if r is None:
            return
        if depth > 6:
            out.append(r); return
        for rows in (True, False):
            parts = split(r, rows)
            # every piece must hold a picture: an empty white slice means we cut through a panel's sky
            if len(parts) > 1 and any(ink[p[1]:p[3], p[0]:p[2]].mean() < P["min_part_ink"] for p in parts):
                continue
            if len(parts) > 1:
                if not rows and rtl:
                    parts = parts[::-1]
                for p in parts:
                    cut(p, depth + 1, out)
                return
        out.append(r)

    out = []
    for b in blocks:
        cut(b, 0, out)
    # specks and slivers (page numbers, a column of text outside the panels)
    kept = [r for r in out if area(r) >= min_area
            and not (min((r[2] - r[0]) / w, (r[3] - r[1]) / h) < 0.07 and area(r) < w * h * 0.04)]
    if len(kept) <= 1:
        return [(0, 0, w, h)]
    if w > h * 1.15:  # a two-page spread in one image: one page, then the other
        right = [r for r in kept if (r[0] + r[2]) / 2 >= w / 2]
        left = [r for r in kept if (r[0] + r[2]) / 2 < w / 2]
        first, second = (right, left) if rtl else (left, right)
        return order(first, rtl) + order(second, rtl)
    return order(kept, rtl)


def _groups(rects, lo, hi):
    """Split rects into runs along an axis wherever there is a gap (small overlaps allowed)."""
    items = sorted(rects, key=lambda r: r[lo])
    groups, cur, end = [], [], None
    for r in items:
        size = r[hi] - r[lo]
        if cur and r[lo] < end - 0.15 * min(size, cur_min):
            cur.append(r); end = max(end, r[hi]); cur_min = min(cur_min, size)
        else:
            if cur:
                groups.append(cur)
            cur, end, cur_min = [r], r[hi], size
    if cur:
        groups.append(cur)
    return groups


def order(rects, rtl=True, depth=0):
    """Reading order: rows top→bottom, columns right→left (manga) — recursively."""
    if len(rects) <= 1 or depth > 8:
        return list(rects)
    rows = _groups(rects, 1, 3)
    if len(rows) > 1:
        return [r for g in rows for r in order(g, rtl, depth + 1)]
    cols = _groups(rects, 0, 2)
    if len(cols) > 1:
        if rtl:
            cols = cols[::-1]
        return [r for g in cols for r in order(g, rtl, depth + 1)]
    # tangled (overlapping boxes): rough rows by top edge, then by side
    span = max(r[3] for r in rects) - min(r[1] for r in rects)
    band = max(1.0, span * 0.12)
    return sorted(rects, key=lambda r: (int(r[1] // band), -r[2] if rtl else r[0]))


def ascii_map(g, rects, cols=64):
    """Coarse layout sketch: ink density + panel frames with their order number."""
    h, w = g.shape
    rows = max(8, int(cols * h / w * 0.5))
    bg, dark = paper(g)
    gi = g.astype(int)
    ink = (gi > bg + 24) if dark else (gi < bg - 24)
    cw, ch = w / cols, h / rows
    grid = []
    for r in range(rows):
        line = []
        for c in range(cols):
            cell = ink[int(r * ch):int((r + 1) * ch), int(c * cw):int((c + 1) * cw)]
            v = cell.mean() if cell.size else 0
            line.append(" .:+%"[min(4, int(v * 5))])
        grid.append(line)
    for k, (x0, y0, x1, y1) in enumerate(rects):
        c0, c1 = int(x0 / cw), min(cols - 1, int((x1 - 1) / cw)); r0, r1 = int(y0 / ch), min(rows - 1, int((y1 - 1) / ch))
        for c in range(c0, c1 + 1):
            grid[r0][c] = "-"; grid[r1][c] = "-"
        for r in range(r0, r1 + 1):
            grid[r][c0] = "|"; grid[r][c1] = "|"
        lbl = str(k)
        for i, chh in enumerate(lbl):
            if c0 + 1 + i < cols:
                grid[r0][c0 + 1 + i] = chh
    return "\n".join("".join(l) for l in grid)
