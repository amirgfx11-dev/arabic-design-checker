"""Build the marked-up report assets: marked page images, finding crops, and a downloadable PDF."""
import os
from PIL import Image, ImageDraw, ImageFont

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")
RED = (220, 38, 38)
ORANGE = (234, 120, 8)
SEV_COL = {"error": RED, "warn": ORANGE, "info": (59, 130, 246)}


def font(size, bold=False, arabic=True):
    # DejaVu Sans covers Arabic AND Latin glyphs, so mixed lines never fall back to empty boxes.
    return ImageFont.truetype(os.path.join(FONTS, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)


def mark_pages(doc, items, outdir, dpi=110):
    """Render pages that have findings with numbered boxes. Sets item['n'] and item['crop']. Returns {page: filename}."""
    os.makedirs(outdir, exist_ok=True)
    by_page = {}
    for n, it in enumerate(items, 1):
        it["n"] = n
        by_page.setdefault(it["page"], []).append(it)
    scale = dpi / 72.0
    pages = {}
    for pg, its in sorted(by_page.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        if doc is None or pg is None or pg > len(doc):
            continue
        im = engine.render_page(doc, pg - 1, dpi)
        clean = im.copy()
        d = ImageDraw.Draw(im, "RGBA")
        for it in its:
            boxes = []
            if it.get("bbox"):
                boxes.append(it["bbox"])
            for pg2, bb2 in it.get("extra", []):
                if pg2 == pg:
                    boxes.append(bb2)
            col = SEV_COL.get(it["severity"], RED)
            for bb in boxes:
                x0, y0, x1, y1 = [v * scale for v in bb]
                pad = 6
                d.rectangle((x0 - pad, y0 - pad, x1 + pad, y1 + pad), outline=col + (255,), width=4, fill=col + (28,))
                d.ellipse((x0 - pad - 30, y0 - pad - 30, x0 - pad + 8, y0 - pad + 8), fill=col + (255,))
                d.text((x0 - pad - 11, y0 - pad - 11), str(it["n"]), font=font(22, True, False), fill=(255, 255, 255), anchor="mm")
        im.save(os.path.join(outdir, f"p{pg}.png"), optimize=True)
        pages[pg] = f"p{pg}.png"
        for it in its:
            if it.get("bbox"):
                own = [it["bbox"]] + [bb2 for pg2, bb2 in it.get("extra", []) if pg2 == pg]
                one = clean.copy()
                d1 = ImageDraw.Draw(one, "RGBA")
                col = SEV_COL.get(it["severity"], RED)
                for bb in own:
                    a0, b0, a1, b1 = [v * scale for v in bb]
                    d1.rectangle((a0 - 6, b0 - 6, a1 + 6, b1 + 6), outline=col + (255,), width=4, fill=col + (28,))
                a0, b0 = it["bbox"][0] * scale, it["bbox"][1] * scale
                d1.ellipse((a0 - 36, b0 - 36, a0 + 2, b0 + 2), fill=col + (255,))
                d1.text((a0 - 17, b0 - 17), str(it["n"]), font=font(22, True, False), fill=(255, 255, 255), anchor="mm")
                ux0 = min(bb[0] for bb in own) * scale; uy0 = min(bb[1] for bb in own) * scale
                ux1 = max(bb[2] for bb in own) * scale; uy1 = max(bb[3] for bb in own) * scale
                mx, my = max(130, (ux1 - ux0) * 0.15), max(90, (uy1 - uy0) * 0.5)
                crop = one.crop((int(max(0, ux0 - mx)), int(max(0, uy0 - my)), int(min(one.width, ux1 + mx)), int(min(one.height, uy1 + my))))
                if crop.width > 900:
                    crop = crop.resize((900, int(crop.height * 900 / crop.width)))
                cn = f"c{it['n']}.png"
                crop.save(os.path.join(outdir, cn), optimize=True)
                it["crop"] = cn
    return pages


def status_rows(stats):
    rows = []
    for key in ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]:
        st = stats.get(key, {"checked": 0, "flagged": 0, "note": ""})
        status = "flagged" if st["flagged"] else ("clean" if st["checked"] > 0 else "not_run")
        rows.append(dict(key=key, name_en=engine.RULES[key][0], name_ar=engine.RULES[key][1], status=status,
                         checked=st["checked"], flagged=st["flagged"], note=st.get("note", "")))
    return rows


# ------------------------------------------------------------------ PDF export (drawn with PIL so Arabic shapes correctly)
PW, PH = 1240, 1754
M = 80


def _dir(s):
    return "rtl" if engine.has_ar(s) else "ltr"


def _wrap(s, f, maxw):
    lines, cur = [], ""
    for w in s.split(" "):
        t = (cur + " " + w).strip()
        if f.getlength(t, direction=_dir(t)) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_pdf(path, meta, items, rows, page_dir):
    pages = []

    def new_page():
        im = Image.new("RGB", (PW, PH), "white")
        return im, ImageDraw.Draw(im), M

    def put(d, y, text, f, fill=(30, 30, 30), maxw=PW - 2 * M, ar=None, indent=0):
        ar = engine.has_ar(text) if ar is None else ar
        for ln in _wrap(text, f, maxw - indent):
            if ar:
                d.text((PW - M - indent, y), ln, font=f, fill=fill, direction="rtl", anchor="ra", language="ar")
            else:
                d.text((M + indent, y), ln, font=f, fill=fill, direction="ltr")
            y += f.size + 10
        return y

    im, d, y = new_page()
    y = put(d, y, "تقرير فحص التصميم العربي", font(54, True), ar=True)
    y = put(d, y, "Arabic Design Checker - Report", font(28, False, False), (90, 90, 90), ar=False)
    y += 10
    y = put(d, y, f"File: {meta['filename']}  |  pages checked: {meta['pages']}  |  {meta['when']}", font(22, False, False), (90, 90, 90), ar=False)
    ne = sum(1 for i in items if i["severity"] == "error")
    nw = sum(1 for i in items if i["severity"] == "warn")
    y += 14
    y = put(d, y, f"النتيجة: {ne} خطأ مؤكد، {nw} تنبيه.", font(40, True), RED if ne else (30, 130, 60), ar=True)
    y += 10
    for r in rows:
        st = {"clean": "سليم", "flagged": f"{r['flagged']} ملاحظة", "not_run": "لم يُختبر"}[r["status"]]
        col = {"clean": (30, 130, 60), "flagged": RED, "not_run": (110, 110, 110)}[r["status"]]
        y = put(d, y, f"{r['name_ar']} ({r['key']}): {st}" + (f" - فُحص {r['checked']}" if r["checked"] else ""), font(28), col, ar=True)
    y += 14
    y = put(d, y, "ملاحظة: الفحص كود ثابت بدون نموذج ذكاء اصطناعي، فنفس الملف يطلع نفس النتيجة دايمًا. الكود يقرأ طبقة النص والبكسلات فقط. اتجاه الأسهم والأيقونات وقراءة الصور المسطحة محتاجة نموذج رؤية، وهو مش في النسخة دي.", font(22), (90, 90, 90), ar=True)
    pages.append(im)
    im, d, y = new_page()
    y = put(d, y, "قائمة الملاحظات", font(44, True), ar=True)
    y += 10
    for it in items:
        if y > PH - M - 200:
            pages.append(im)
            im, d, y = new_page()
        col = SEV_COL.get(it["severity"], RED)
        d.ellipse((M, y + 4, M + 44, y + 48), fill=col)
        d.text((M + 22, y + 26), str(it["n"]), font=font(26, True, False), fill="white", anchor="mm")
        where = f"صفحة {it['page']}" if it.get("page") else ""
        if it.get("loc"):
            where += f" ({it['loc']})"
        y = put(d, y, f"{engine.RULES[it['check']][1]} - {where}", font(28, True), col, maxw=PW - 2 * M - 70, ar=True)
        y = put(d, y, it["message_ar"], font(26), maxw=PW - 2 * M - 70, ar=True)
        y = put(d, y, it["message_en"], font(19, False, False), (90, 90, 90), maxw=PW - 2 * M - 70, ar=False, indent=70)
        y = put(d, y, f"method: {it['method']}  |  confidence: {it['confidence']}", font(17, False, False), (130, 130, 130), maxw=PW - 2 * M - 70, ar=False, indent=70)
        y += 14
    pages.append(im)
    for pg in sorted({i["page"] for i in items if i.get("page")}):
        fp = os.path.join(page_dir, f"p{pg}.png")
        if not os.path.exists(fp):
            continue
        src = Image.open(fp).convert("RGB")
        im = Image.new("RGB", (PW, PH), "white")
        d = ImageDraw.Draw(im)
        w = PW - 2 * M
        src = src.resize((w, int(src.height * w / src.width)))
        d.text((PW - M, M - 50), f"صفحة {pg}", font=font(30, True), fill=(30, 30, 30), direction="rtl", anchor="ra", language="ar")
        im.paste(src, (M, M))
        yy = M + 20 + src.height
        for it in [i for i in items if i["page"] == pg]:
            if yy > PH - M - 40:
                break
            col = SEV_COL.get(it["severity"], RED)
            d.ellipse((M, yy, M + 36, yy + 36), fill=col)
            d.text((M + 18, yy + 18), str(it["n"]), font=font(20, True, False), fill="white", anchor="mm")
            d.text((PW - M, yy), it["message_ar"][:150], font=font(22), fill=(30, 30, 30), direction="rtl", anchor="ra", language="ar")
            yy += 46
        pages.append(im)
    pages[0].save(path, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
