"""Text layer vs rendered page (PDF). Deterministic, no model.

Some PDF exporters write a text layer that differs from what is drawn (letters swapped, ligature order scrambled).
Every text-based rule trusts the text layer, so a mismatch would silently give a wrong result.

Method: for each Arabic word, re-draw the extracted text (Raqm shaping) and compare its ink with the rendered page.
Then re-draw every one-letter-swapped variant of the word. If a variant matches the page clearly better than the
extracted word, the text layer disagrees with what is drawn. The embedded font is used when it can be extracted
(exact), otherwise Noto Sans Arabic as a stand-in (relative comparison, so the font difference cancels out).
A second cheap signal counts extraction garbage (control / replacement characters inside Arabic text).
"""
import io
import re
import time
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import fitz

FALLBACK = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"
FALLBACK_B = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
AR_BASE = re.compile("[\u0621-\u064A]")
GARBAGE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffd]")
W, H = 128, 40
MARGIN = 0.18        # a swapped variant must beat the extracted word by this much
MIN_FALLBACK = 0.55  # ...and match the page at least this well (stand-in font)
MIN_EMBEDDED = 0.85  # ...with the page's own font
MAX_WORDS = 320
BUDGET_S = 25.0


def _fn(n):
    return re.sub(r"[^a-z0-9]", "", n.split("+")[-1].lower())


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFKC", s) if AR_BASE.match(c))


def _feat(img):
    m = np.array(img.convert("L"), dtype=np.float32) < 140
    ys, xs = np.where(m)
    if len(ys) < 8:
        return None
    t = Image.fromarray((m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype("uint8")).resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.6))
    v = np.array(t, dtype=np.float32)
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n else None


def _draw(font, word):
    im = Image.new("L", (120 * len(word) + 80, 260), 255)
    ImageDraw.Draw(im).text((im.width - 20, 20), word, font=font, fill=0, anchor="ra", direction="rtl", language="ar")
    return _feat(im)


def _variants(w):
    return [w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(len(w) - 1) if w[i] != w[i + 1]]


def _words(page):
    res = []
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                cur = []
                for c in s["chars"] + [{"c": " ", "bbox": (0, 0, 0, 0)}]:
                    if c["c"].isspace():
                        if cur:
                            t = "".join(x["c"] for x in cur)
                            bb = (min(x["bbox"][0] for x in cur), min(x["bbox"][1] for x in cur), max(x["bbox"][2] for x in cur), max(x["bbox"][3] for x in cur))
                            res.append((t, bb, s["font"], (s["flags"] & 16) > 0 or bool(re.search(r"bold|heavy|black", s["font"], re.I))))
                            cur = []
                    else:
                        cur.append(c)
    return res


def verify_pdf(doc, n_pages, outdir=None):
    """Returns dict(state: ok|suspect|unverified, checked, flagged, mode, garbage, words[...], reasons[...])."""
    t0 = time.time()
    out = dict(state="ok", checked=0, flagged=0, exact=0, garbage=0, words=[], reasons=[], mode="")
    # ---- signal 2: extraction garbage inside Arabic text
    cand = []
    for pn in range(min(n_pages, len(doc))):
        try:
            p = doc[pn]
            txt = p.get_text()
        except Exception:
            continue
        for ln in txt.split("\n"):
            if AR_BASE.search(unicodedata.normalize("NFKC", ln)):
                out["garbage"] += len(GARBAGE.findall(ln))
        for t, bb, font, bold in _words(p):
            w = _norm(t)
            if len(w) >= 3 and bb[2] - bb[0] > 5 and bb[3] - bb[1] > 5:
                cand.append((pn, t, w, bb, font, bold))
    # ---- signal 1: re-draw vs page, evenly sampled across the file
    if len(cand) > MAX_WORDS:
        step = len(cand) / MAX_WORDS
        cand = [cand[int(i * step)] for i in range(MAX_WORDS)]
    fonts_cache, fb_cache, emb_fonts = {}, {}, {}
    pix_cache = {}

    def page_img(pn, Z=4):
        if pn not in pix_cache:
            if len(pix_cache) > 3:
                pix_cache.clear()
            pm = doc[pn].get_pixmap(matrix=fitz.Matrix(Z, Z), colorspace=fitz.csGRAY)
            pix_cache[pn] = Image.frombytes("L", (pm.width, pm.height), pm.samples)
        return pix_cache[pn]

    def fb_font(bold):
        if bold not in fb_cache:
            fb_cache[bold] = ImageFont.truetype(FALLBACK_B if bold else FALLBACK, 120, layout_engine=ImageFont.Layout.RAQM)
        return fb_cache[bold]

    def emb_font(pn, fname):
        key = (pn, fname)
        if key in emb_fonts:
            return emb_fonts[key]
        f = None
        try:
            for fi in doc[pn].get_fonts():
                if _fn(fi[3]) == _fn(fname):
                    r = doc.extract_font(fi[0])
                    if r[1] in ("ttf", "otf") and r[3]:
                        f = ImageFont.truetype(io.BytesIO(r[3]), 120, layout_engine=ImageFont.Layout.RAQM)
                    break
        except Exception:
            f = None
        emb_fonts[key] = f
        return f

    rcache = {}

    def score(fkey, font, fp, word):
        k = (fkey, word)
        if k not in rcache:
            try:
                rcache[k] = _draw(font, word)
            except Exception:
                rcache[k] = None
        r = rcache[k]
        return -1.0 if r is None else float((fp * r).sum())

    for pn, t, w, bb, fname, bold in cand:
        if time.time() - t0 > BUDGET_S:
            break
        Z = 4
        x0, y0, x1, y1 = [v * Z for v in bb]
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        pg = page_img(pn)
        crop = pg.crop((int(x0) - 2, int(y0) - 2, int(x1) + 2, int(y1) + 2))
        crop0 = pg.crop((int(x0), int(y0), int(x1), int(y1)))  # exact box: neighbours stay out (embedded font path)
        if np.array(crop).mean() < 120:  # light text on a dark background: ink model does not apply
            continue
        fp = _feat(crop)
        fp0 = _feat(crop0)
        if fp is None or fp0 is None:
            continue
        vs = _variants(w)
        res = None
        ef = emb_font(pn, fname)
        if ef is not None:
            s0 = score(("e", fname), ef, fp0, w)
            sv = [(score(("e", fname), ef, fp0, v), v) for v in vs]
            best = max([(s0, w)] + sv)
            if best[0] >= MIN_EMBEDDED:
                res = ("embedded", s0, max(sv) if sv else (-1, ""))
        if res is None:
            fbf = fb_font(bold)
            s0 = score(("f", bold), fbf, fp, w)
            sv = [(score(("f", bold), fbf, fp, v), v) for v in vs]
            res = ("fallback", s0, max(sv) if sv else (-1, ""))
        mode, s0, (bs, bv) = res
        out["checked"] += 1
        thr = MIN_EMBEDDED if mode == "embedded" else MIN_FALLBACK
        if bs - s0 >= MARGIN and bs >= thr:
            out["flagged"] += 1
            out["exact"] += mode == "embedded"
            if len(out["words"]) < 40:
                d = dict(page=pn + 1, text=w, closer=bv, mode=mode, score=round(s0, 2), alt=round(bs, 2), crop=None)
                if outdir and len(out["words"]) < 6:
                    try:
                        import os
                        os.makedirs(outdir, exist_ok=True)
                        name = "tl_%d.png" % len(out["words"])
                        crop.resize((crop.width * 2, crop.height * 2)).save(os.path.join(outdir, name)) if crop.width < 400 else crop.save(os.path.join(outdir, name))
                        d["crop"] = name
                    except Exception:
                        pass
                out["words"].append(d)
    modes = {x["mode"] for x in out["words"]}
    out["mode"] = "embedded" if modes == {"embedded"} else "fallback" if modes else ""
    out["seconds"] = round(time.time() - t0, 1)
    if out["exact"] >= 1:
        out["reasons"].append("exact")
    if out["flagged"] >= 3 and out["flagged"] >= 0.01 * max(1, out["checked"]):
        out["reasons"].append("many")
    if out["garbage"] >= 3:
        out["reasons"].append("garbage")
    if out["reasons"]:
        out["state"] = "suspect"
    elif out["checked"] == 0 and cand:
        out["state"] = "unverified"
    return out
