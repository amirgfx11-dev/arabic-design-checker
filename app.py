"""Arabic Design Checker - V1 web app (Flask). No accounts, no payments, no external services."""
import io, json, os, secrets, shutil, tempfile, time, zipfile, hmac
from datetime import datetime
from flask import (Flask, abort, redirect, render_template, request, send_file, session, url_for, make_response)

import fitz
from PIL import Image

import csv, re, engine, fixes, report, autofix, scoring

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


AR_RE = re.compile("[\u0621-\u064A\u066E-\u06D3\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


class JobError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


def run_job(save, fname, pasted, form, files, ocr):
    """Runs one check. save(dst) writes the uploaded file to dst (None when only text is pasted). Returns the job id."""
    jid = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
    jdir = os.path.join(DATA, jid)
    os.makedirs(jdir)
    work = os.path.join(jdir, "work")
    os.makedirs(work)
    t0 = time.time()
    F = engine.Findings()
    brand = build_brand(form, files, work)
    doc = None
    n_pages = 1
    kind = "text"
    pptx_real = False
    ar_n = 0
    try:
        if save:
            fname = os.path.basename(fname)
            ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
            src = os.path.join(work, "input." + ext)
            save(src)
            if ext == "pdf":
                kind = "pdf"
                doc, n_pages, _ = engine.analyze_pdf(src, F, brand, max_pages=MAX_PAGES)
                try:
                    ar_n = sum(len(AR_RE.findall(pg.get_text())) for pg in doc)
                except Exception:
                    ar_n = 1
                engine.pixel_checks_pdf(doc, n_pages, F, brand, ocr=ocr)
                engine.consistency_pdf(doc, n_pages, F)
            elif ext in ("png", "jpg", "jpeg", "webp"):
                kind = "image"
                img = Image.open(src).convert("RGB")
                engine.analyze_image_pixels(img, F, brand)
                doc = fitz.open(stream=_img_to_pdf_bytes(img), filetype="pdf")
            elif ext == "pptx":
                kind = "pptx"
                n_pages = engine.analyze_pptx(src, F, brand)
                try:
                    from pptx import Presentation
                    ar_n = sum(len(AR_RE.findall(tf.text)) for _s, _n, _k, tf in autofix._frames(Presentation(src)))
                except Exception:
                    ar_n = 1
                try:
                    shutil.copy(src, os.path.join(jdir, "original.pptx"))
                    json.dump(autofix.generate(src, brand), open(os.path.join(jdir, "corrections.json"), "w"), ensure_ascii=False)
                except Exception:
                    pass  # auto-correction is optional; the report still works
                doc = engine.pptx_render_doc(src, F.pptx, jdir) if F.pptx else None
                pptx_real = doc is not None
                if F.pptx and doc is None:
                    doc = engine.pptx_wireframe_doc(F.pptx)
            else:
                shutil.rmtree(jdir, ignore_errors=True)
                raise JobError("نوع الملف غير مدعوم. الأنواع المدعومة: PDF وPPTX وPNG وJPG.")
        else:
            fname = "نص ملصق"
        if pasted:
            engine.analyze_text(pasted, F, brand)
            ar_n += len(AR_RE.findall(pasted))
    except JobError:
        raise
    except Exception as e:  # unreadable / corrupt file
        shutil.rmtree(jdir, ignore_errors=True)
        raise JobError("تعذّرت قراءة الملف (%s). جرّب تصديره بصيغة PDF مرة أخرى." % type(e).__name__)
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
    applied = sum(1 for r_ in rows if r_["status"] != "not_run")
    sc = scoring.compute(items, engine.RULES, n_pages, applied, len(rows), ar_n >= 3, kind)
    meta = dict(id=jid, filename=fname, kind=kind, pptx_real=pptx_real, pages=n_pages, when=datetime.now().strftime("%Y-%m-%d %H:%M"),
                seconds=round(time.time() - t0, 1), brand=brand.summary() if brand.active() else None,
                pasted=bool(pasted), ocr=ocr, tone=F.tone, score=sc)
    report.build_pdf(os.path.join(jdir, "report.pdf"), meta, items, rows, pdir)
    json.dump(dict(meta=meta, items=items, rows=rows, pages=pages), open(os.path.join(jdir, "result.json"), "w"), ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)  # the uploaded design is deleted right after the check
    return jid


@app.route("/check", methods=["POST"])
def check():
    cleanup()
    up = request.files.get("design")
    pasted = request.form.get("pasted", "").strip()
    has = bool(up and up.filename)
    if not has and not pasted:
        return render_template("index.html", error="يرجى رفع ملف تصميم أو لصق نص للفحص.", ocr=engine.tesseract_available(), max_pages=MAX_PAGES), 400
    try:
        jid = run_job(up.save if has else None, up.filename if has else None, pasted, request.form, request.files, bool(request.form.get("ocr")))
    except JobError as e:
        return render_template("index.html", error=e.msg, ocr=engine.tesseract_available(), max_pages=MAX_PAGES), e.code
    return redirect(url_for("result", jid=jid))


# ---------- batch ----------
MAX_BATCH = 10
BATCH_EXT = ("pdf", "pptx", "png", "jpg", "jpeg", "webp")
KIND_AR = {"pdf": "PDF", "pptx": "PPTX", "image": "صورة", "text": "نص"}


def _bpath(bid):
    if not bid.isalnum():
        abort(404)
    return os.path.join(DATA, "batch" + bid)


def _bsave(bid, st):
    p = os.path.join(_bpath(bid), "batch.json")
    json.dump(st, open(p + ".tmp", "w"), ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _bload(bid):
    p = os.path.join(_bpath(bid), "batch.json")
    if not os.path.exists(p):
        abort(404)
    return json.load(open(p))


def _batch_worker(bid):
    st = _bload(bid)
    for i, it in enumerate(st["items"]):
        if it["status"] != "queued":
            continue
        it["status"] = "running"
        _bsave(bid, st)
        path = os.path.join(_bpath(bid), "in", it["file"])
        try:
            jid = run_job(lambda dst, p=path: shutil.copy(p, dst), it["name"], "", {}, {}, False)
            r = load(jid)
            its = r["items"]
            it.update(status="done", jid=jid, kind=r["meta"]["kind"], pages=r["meta"]["pages"], score=r["meta"]["score"]["value"], applied=r["meta"]["score"].get("applied"), total=r["meta"]["score"].get("total"),
                      errors=sum(1 for x in its if x["severity"] == "error"), warns=sum(1 for x in its if x["severity"] == "warn"))
        except JobError as e:
            it.update(status="error", msg=e.msg)
        except Exception as e:
            it.update(status="error", msg="تعذّر فحص الملف (%s)." % type(e).__name__)
        _bsave(bid, st)
    st["done"] = True
    _bsave(bid, st)


@app.route("/batch", methods=["POST"])
def batch():
    cleanup()
    fs = [f for f in request.files.getlist("designs") if f.filename]
    err = None
    if not fs:
        err = "اختر ملفًا واحدًا على الأقل."
    elif len(fs) > MAX_BATCH:
        err = "الحد الأقصى %d ملفات في الدفعة الواحدة." % MAX_BATCH
    if err:
        return render_template("index.html", error=err, ocr=engine.tesseract_available(), max_pages=MAX_PAGES), 400
    bid = secrets.token_hex(8)
    os.makedirs(os.path.join(_bpath(bid), "in"))
    items = []
    for i, f in enumerate(fs):
        name = os.path.basename(f.filename)
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext not in BATCH_EXT:
            items.append(dict(name=name, file="", status="error", msg="نوع الملف غير مدعوم."))
            continue
        fn = "%d.%s" % (i, ext)
        f.save(os.path.join(_bpath(bid), "in", fn))
        items.append(dict(name=name, file=fn, status="queued"))
    st = dict(id=bid, created=time.time(), when=datetime.now().strftime("%Y-%m-%d %H:%M"), items=items, done=False)
    if not any(i["status"] == "queued" for i in items):
        st["done"] = True
    _bsave(bid, st)
    if not st["done"]:
        import threading
        threading.Thread(target=_batch_worker, args=(bid,), daemon=True).start()
    return redirect(url_for("batch_view", bid=bid))


def _batch_stats(st):
    done = [i for i in st["items"] if i["status"] == "done"]
    stale = (not st["done"]) and time.time() - os.path.getmtime(os.path.join(_bpath(st["id"]), "batch.json")) > 600
    for it in done:
        if it["score"] is None:
            it.update(band_ar="غير منطبق", color="#888")
        else:
            key, ar, color = scoring.band(it["score"])
            it.update(band_ar=ar, color=color)
        cj = os.path.join(DATA, it["jid"], "corrections.json")
        it["fixable"] = 0
        if os.path.exists(cj):
            try:
                it["fixable"] = sum(1 for c in json.load(open(cj))["corrections"] if c.get("default_on"))
            except Exception:
                pass
    scored = [i for i in done if i["score"] is not None]
    avg = round(sum(i["score"] for i in scored) / len(scored)) if scored else None
    common = {}
    for it in done:
        try:
            for x in load(it["jid"])["items"]:
                if x["severity"] in ("error", "warn"):
                    g = common.setdefault(x["check"], dict(name=engine.RULES[x["check"]][1], n=0, files=set()))
                    g["n"] += 1
                    g["files"].add(it["jid"])
        except Exception:
            pass
    common = sorted(({"name": g["name"], "n": g["n"], "files": len(g["files"])} for g in common.values()), key=lambda g: -g["n"])[:5]
    return dict(done=done, scored=len(scored), avg=avg, stale=stale, common=common,
                errors=sum(i["errors"] for i in done), warns=sum(i["warns"] for i in done),
                finished=sum(1 for i in st["items"] if i["status"] in ("done", "error")),
                fixable=sum(i["fixable"] for i in done),
                avg_band=scoring.band(avg) if avg is not None else None)


@app.route("/b/<bid>")
def batch_view(bid):
    st = _bload(bid)
    stats = _batch_stats(st)
    items = list(st["items"])
    if request.args.get("sort") == "score":
        items.sort(key=lambda i: (i["status"] != "done", i.get("score") is None, i.get("score") or 0))
    return render_template("batch.html", st=st, items=items, s=stats, sort=request.args.get("sort", ""), kind_ar=KIND_AR,
                           live=(not st["done"]) and not stats["stale"])


@app.route("/b/<bid>/summary.csv")
def batch_csv(bid):
    st = _bload(bid)
    _batch_stats(st)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file", "type", "pages", "score_0_100", "band", "errors", "warnings", "auto_fixable", "status"])
    for i in st["items"]:
        if i["status"] == "done":
            w.writerow([i["name"], i["kind"], i["pages"], "" if i["score"] is None else i["score"], i["band_ar"], i["errors"], i["warns"], i["fixable"], "ok"])
        else:
            w.writerow([i["name"], "", "", "", "", "", "", "", i.get("msg", i["status"])])
    resp = make_response("\ufeff" + buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=batch-summary.csv"
    return resp


@app.route("/b/<bid>/fixed.zip")
def batch_zip(bid):
    st = _bload(bid)
    buf = io.BytesIO()
    n = 0
    tmp = os.path.join(_bpath(bid), "fixed")
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in st["items"]:
            if i["status"] != "done" or i["kind"] != "pptx":
                continue
            jd = os.path.join(DATA, i["jid"])
            try:
                corr = json.load(open(os.path.join(jd, "corrections.json")))
            except Exception:
                continue
            ids = [c["id"] for c in corr["corrections"] if c.get("default_on")]
            if not ids:
                continue
            out = os.path.join(tmp, i["jid"] + ".pptx")
            autofix.apply(os.path.join(jd, "original.pptx"), out, ids, corr.get("numeral_target"), corr.get("tone_dom"))
            z.write(out, os.path.splitext(i["name"])[0] + "-corrected.pptx")
            n += 1
    if not n:
        abort(404)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="corrected-pptx-files.zip")


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
    cj = os.path.join(DATA, jid, "corrections.json")
    corr = json.load(open(cj)) if os.path.exists(cj) else None
    kinds = []
    if corr:
        for k, (nar, nen, on) in autofix.KINDS.items():
            its = [c for c in corr["corrections"] if c["kind"] == k]
            if its:
                kinds.append(dict(key=k, name=nar, name_en=nen, its=its, on=on))
    return render_template("report.html", corr=corr, ckinds=kinds, r=r, pages=pages, by_page=by_page, ne=ne, nw=nw, rules=engine.RULES, groups=groups)


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


def _selected(jid):
    r = load(jid)
    cj = os.path.join(DATA, jid, "corrections.json")
    if r["meta"].get("kind") != "pptx" or not os.path.exists(cj) or not os.path.exists(os.path.join(DATA, jid, "original.pptx")):
        abort(404)
    corr = json.load(open(cj))
    ids = set(request.form.getlist("c"))
    valid = {c["id"] for c in corr["corrections"]}
    return r, corr, [i for i in ids if i in valid]


@app.route("/r/<jid>/fixed.pptx", methods=["POST"])
def fixed_pptx(jid):
    r, corr, ids = _selected(jid)
    if not ids:
        return redirect(url_for("result", jid=jid) + "#autofix")
    out = os.path.join(DATA, jid, "fixed.pptx")
    autofix.apply(os.path.join(DATA, jid, "original.pptx"), out, ids, corr.get("numeral_target"), corr.get("tone_dom"))
    base = os.path.splitext(r["meta"]["filename"])[0]
    return send_file(out, as_attachment=True, download_name=base + "-corrected.pptx",
                     mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@app.route("/r/<jid>/corrections.csv", methods=["POST"])
def corrections_csv(jid):
    r, corr, ids = _selected(jid)
    ids = set(ids)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["slide", "shape", "type", "before", "after", "context", "reason", "method", "confidence"])
    for c in corr["corrections"]:
        if c["id"] in ids:
            w.writerow([c["slide"], c["shape"], autofix.KINDS[c["kind"]][1], c["before"], c["after"], c["ctx_before"] + "[" + c["before"] + "]" + c["ctx_after"],
                        c["note_en"], c["method"], c["confidence"]])
    resp = make_response("\ufeff" + buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=corrections-log.csv"
    return resp


@app.route("/r/<jid>/findings.json")
def fjson(jid):
    r = load(jid)
    resp = make_response(json.dumps(dict(meta=r["meta"], findings=r["items"], rules=r["rows"]), ensure_ascii=False, indent=1))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
