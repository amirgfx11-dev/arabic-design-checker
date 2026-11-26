"""Arabic Design Checker - V1 web app (Flask). No accounts, no payments, no external services."""
import io, json, os, secrets, shutil, tempfile, time, zipfile, hmac
from datetime import datetime
from flask import (Flask, abort, redirect, render_template, request, send_file, session, url_for, make_response)

import fitz
from PIL import Image

import engine, fixes, report

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
DATA = os.environ.get("DATA_DIR") or os.path.join(tempfile.gettempdir(), "arabic-checker")
os.makedirs(DATA, exist_ok=True)
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()   # optional: set it to require a shared code
KEEP_SECONDS = int(os.environ.get("KEEP_SECONDS", "7200"))  # reports are deleted after 2h by default
MAX_PAGES = int(os.environ.get("MAX_PAGES", "60"))


def cleanup():
    now = time.time()
    for n in os.listdir(DATA):
        p = os.path.join(DATA, n)
        try:
            if now - os.path.getmtime(p) > KEEP_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


@app.before_request
def gate():
    if request.endpoint in ("healthz", "static", "login"):
        return
    if ACCESS_CODE and not session.get("ok"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    err = False
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("code", "").strip(), ACCESS_CODE):
            session["ok"] = True
            return redirect(request.args.get("next") or "/")
        err = True
    return render_template("login.html", err=err)


@app.route("/healthz")
def healthz():
    return "ok"


@app.template_filter("method_ar")
def method_ar(m):
    m = m or ""
    if m.startswith("code + OCR"):
        return "كود + قراءة OCR"
    if m.startswith("code"):
        return "كود ثابت"
    return "بمساعدة نموذج"


@app.route("/")
def index():
    return render_template("index.html", ocr=engine.tesseract_available(), max_pages=MAX_PAGES)


def build_brand(form, files, tmpdir):
    fonts = [f.strip() for f in form.get("fonts", "").replace("،", ",").split(",") if f.strip()]
    colors = engine.parse_hex_list(form.get("colors", ""))
    numerals = form.get("numerals", "any")
    try:
        min_size = float(form.get("min_size") or 0) or None
    except ValueError:
        min_size = None
    notes = []
    g = files.get("guidelines")
    parsed = None
    if g and g.filename:
        gp = os.path.join(tmpdir, "guidelines.pdf")
        g.save(gp)
        try:
            parsed = engine.parse_guidelines_pdf(gp)
        except Exception:
            notes.append("تعذّر قراءة ملف الدليل (PDF).")
    if parsed:
        fonts = fonts or parsed.fonts
        colors = colors or parsed.colors
        if numerals == "any":
            numerals = parsed.numerals
        notes.append(parsed.source_note)
    b = engine.Brand(fonts, colors, numerals, min_size, " ".join(notes))
    return b


@app.route("/check", methods=["POST"])
def check():
    cleanup()
    up = request.files.get("design")
    pasted = request.form.get("pasted", "").strip()
    if (not up or not up.filename) and not pasted:
        return render_template("index.html", error="يرجى رفع ملف تصميم أو لصق نص للفحص.", ocr=engine.tesseract_available(), max_pages=MAX_PAGES), 400
    jid = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
    jdir = os.path.join(DATA, jid)
    os.makedirs(jdir)
    work = os.path.join(jdir, "work")
    os.makedirs(work)
    t0 = time.time()
    F = engine.Findings()
    brand = build_brand(request.form, request.files, work)
    doc = None
    n_pages = 1
    fname = "نص ملصق"
    kind = "text"
    pptx_real = False
    try:
        if up and up.filename:
            fname = os.path.basename(up.filename)
            ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
            src = os.path.join(work, "input." + ext)
            up.save(src)
            if ext == "pdf":
                kind = "pdf"
                doc, n_pages, _ = engine.analyze_pdf(src, F, brand, max_pages=MAX_PAGES)
                engine.pixel_checks_pdf(doc, n_pages, F, brand, ocr=bool(request.form.get("ocr")))
                engine.consistency_pdf(doc, n_pages, F)
            elif ext in ("png", "jpg", "jpeg", "webp"):
                kind = "image"
                img = Image.open(src).convert("RGB")
                engine.analyze_image_pixels(img, F, brand)
                doc = fitz.open(stream=_img_to_pdf_bytes(img), filetype="pdf")
            elif ext == "pptx":
                kind = "pptx"
                n_pages = engine.analyze_pptx(src, F, brand)
                doc = engine.pptx_render_doc(src, F.pptx, jdir) if F.pptx else None
                pptx_real = doc is not None
                if F.pptx and doc is None:
                    doc = engine.pptx_wireframe_doc(F.pptx)
            else:
                shutil.rmtree(jdir, ignore_errors=True)
                return render_template("index.html", error="نوع الملف غير مدعوم. الأنواع المدعومة: PDF وPPTX وPNG وJPG.", ocr=engine.tesseract_available(), max_pages=MAX_PAGES), 400
        if pasted:
            engine.analyze_text(pasted, F, brand)
            if kind == "text":
                kind = "text"
    except Exception as e:  # unreadable / corrupt file
        shutil.rmtree(jdir, ignore_errors=True)
        return render_template("index.html", error="تعذّرت قراءة الملف (%s). جرّب تصديره بصيغة PDF مرة أخرى." % type(e).__name__, ocr=engine.tesseract_available(), max_pages=MAX_PAGES), 400
    items = sorted(F.items, key=lambda i: ((i["page"] or 0), {"error": 0, "warn": 1, "info": 2}[i["severity"]]))
    for it in items:
        if not it.get("fix_ar"):
            try:
                a, e = fixes.suggest(it, brand)
            except Exception:
                a, e = None, None
            if a:
                it["fix_ar"], it["fix_en"] = a, e
    pdir = os.path.join(jdir, "pages")
    pages = report.mark_pages(doc, items, pdir)
    rows = report.status_rows(dict(F.stats))
    meta = dict(id=jid, filename=fname, kind=kind, pptx_real=pptx_real, pages=n_pages, when=datetime.now().strftime("%Y-%m-%d %H:%M"),
                seconds=round(time.time() - t0, 1), brand=brand.summary() if brand.active() else None,
                pasted=bool(pasted), ocr=bool(request.form.get("ocr")), tone=F.tone)
    report.build_pdf(os.path.join(jdir, "report.pdf"), meta, items, rows, pdir)
    json.dump(dict(meta=meta, items=items, rows=rows, pages=pages), open(os.path.join(jdir, "result.json"), "w"), ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)  # the uploaded design is deleted right after the check
    return redirect(url_for("result", jid=jid))


def _img_to_pdf_bytes(img):
    b = io.BytesIO()
    img.save(b, "PDF", resolution=72.0)
    return b.getvalue()


def load(jid):
    if not jid.replace("-", "").replace("_", "").isalnum():
        abort(404)
    p = os.path.join(DATA, jid, "result.json")
    if not os.path.exists(p):
        abort(404)
    return json.load(open(p))


@app.route("/r/<jid>")
def result(jid):
    r = load(jid)
    pages = {int(k): v for k, v in r["pages"].items()}
    by_page = {}
    for it in r["items"]:
        by_page.setdefault(it["page"], []).append(it)
    ne = sum(1 for i in r["items"] if i["severity"] == "error")
    nw = sum(1 for i in r["items"] if i["severity"] == "warn")
    gd = {}
    for it in r["items"]:
        g = gd.setdefault(it["check"], dict(key=it["check"], name=engine.RULES[it["check"]][1], its=[], errors=0))
        g["its"].append(it)
        g["errors"] += it["severity"] == "error"
    groups = sorted(gd.values(), key=lambda g: (-g["errors"], -len(g["its"])))
    return render_template("report.html", r=r, pages=pages, by_page=by_page, ne=ne, nw=nw, rules=engine.RULES, groups=groups)


@app.route("/r/<jid>/img/<name>")
def img(jid, name):
    load(jid)
    if "/" in name or ".." in name or not name.endswith(".png"):
        abort(404)
    p = os.path.join(DATA, jid, "pages", name)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/png")


@app.route("/r/<jid>/report.pdf")
def pdf(jid):
    load(jid)
    return send_file(os.path.join(DATA, jid, "report.pdf"), mimetype="application/pdf", as_attachment=True, download_name="arabic-design-report.pdf")


@app.route("/r/<jid>/findings.json")
def fjson(jid):
    r = load(jid)
    resp = make_response(json.dumps(dict(meta=r["meta"], findings=r["items"], rules=r["rows"]), ensure_ascii=False, indent=1))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
