"""Auto-correction for PPTX: safe, traceable fixes applied to text runs in place.
Every fix is code (curated patterns, character maps, word lists). Nothing here uses a language model.
generate() lists the fixes with stable ids; apply() writes a corrected copy with only the chosen ids."""
import re
import shutil
from collections import Counter

import engine
import tone

AR = re.compile(r"[\u0621-\u064A]")
LAT = re.compile(r"[A-Za-z]")
INDIC, WEST, PERSIAN = engine.INDIC, engine.WEST, engine.PERSIAN
TASH = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
EDGE = "،؛؟!.:()[]{}«»\"'“”-–—/\\|…,;?"

KINDS = {
    "spell": ("الإملاء والهمزات", "Spelling and hamza forms", True),
    "letters": ("الحروف الفارسية", "Persian letter forms", True),
    "digits": ("توحيد نظام الأرقام", "Numeral system", True),
    "punct": ("علامات الترقيم والمسافات", "Punctuation and spacing", True),
    "rtl": ("اتجاه الفقرة (RTL)", "Paragraph direction (RTL)", True),
    "align": ("محاذاة الفقرة إلى اليمين", "Right alignment", True),
    "tone": ("تبديل الكلمات حسب النبرة", "Tone word swaps", False),
}


def _frames(prs):
    """Deterministic walk: yields (slide_no, shape_name, key, text_frame)."""
    def walk(shapes):
        for sh in shapes:
            if sh.shape_type == 6:
                yield from walk(sh.shapes)
            else:
                yield sh
    for si, slide in enumerate(prs.slides, 1):
        for shi, sh in enumerate(walk(slide.shapes)):
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                yield si, sh.name, f"{si}.{shi}", sh.text_frame
            if getattr(sh, "has_table", False) and sh.has_table:
                for ri, row in enumerate(sh.table.rows):
                    for ci, cell in enumerate(row.cells):
                        yield si, f"{sh.name} (جدول {ri + 1}:{ci + 1})", f"{si}.{shi}.t{ri}_{ci}", cell.text_frame


def _para_is_ar(text):
    a, l = len(AR.findall(text)), len(LAT.findall(text))
    return a >= 3 and a > l


def _conv(s, target):
    tbl = INDIC if target == "indic" else WEST
    out = []
    for c in s:
        for src in (INDIC, WEST, PERSIAN):
            if c in src:
                out.append(tbl[src.index(c)]); break
        else:
            out.append(c)
    return "".join(out)


def _numeral_target(prs, pref):
    if pref in ("indic", "west"):
        return pref, "دليل الهوية"
    cnt = Counter()
    for _, _, _, tf in _frames(prs):
        for p in tf.paragraphs:
            t = "".join(r.text for r in p.runs)
            if _para_is_ar(t):
                for tok in re.findall(r"\S+", t):
                    for k in engine.digit_kind(tok):
                        cnt[k] += 1
    if (cnt["indic"] or cnt["persian"]) and cnt["west"]:
        return ("indic" if cnt["indic"] + cnt["persian"] >= cnt["west"] else "west"), "أغلبية الأرقام في الملف"
    if cnt["persian"] and not cnt["west"]:
        return "indic", "أغلبية الأرقام في الملف"
    return None, ""


def _tone_dominant(prs):
    toks = []
    class T:  # light token
        def __init__(s, t): s.text = t
    for _, _, _, tf in _frames(prs):
        for p in tf.paragraphs:
            t = "".join(r.text for r in p.runs)
            if _para_is_ar(t) or AR.search(t):
                toks += [T(w) for w in re.findall(r"\S+", engine.nfkc(t))]
    ar = [t for t in toks if AR.search(t.text) or tone.EMOJI.search(t.text)]
    if len(ar) < 8:
        return None
    hits = tone.classify(ar)
    nf = sum(1 for _, k, _ in hits if k == "friendly")
    nm = sum(1 for _, k, _ in hits if k == "formal")
    if nf + nm < 3:
        return None
    share = nm / (nf + nm)
    if share >= 0.65 and nm >= 2:
        return "formal"
    if (1 - share) >= 0.65 and nf >= 2:
        return "friendly"
    return None


def _counts(text):
    t = engine.nfkc(text)
    return len(engine.AR_LETTER.findall(t)), len(engine.LAT_LETTER.findall(t))


def _is_placeholder(tf):
    try:
        return bool(tf._txBody.getparent().xpath(".//p:nvPr/p:ph"))
    except Exception:
        return False


def _group_mixed(prs):
    """shape/table key -> True when it holds a Latin-dominant paragraph (so left alignment may be intended)."""
    mixed = {}
    for si, name, key, tf in _frames(prs):
        g = key.split(".t")[0]
        for p in tf.paragraphs:
            a, l = _counts("".join(r.text for r in p.runs))
            if l >= 3 and l > a:
                mixed[g] = True
    return mixed


def _para_edits(p, tf, key, mixed):
    """Paragraph-level fixes. Returns list of (suffix, kind, before, after, note_ar, note_en, confidence) and a skip flag."""
    text = "".join(r.text for r in p.runs)
    a, l = _counts(text)
    out, skipped = [], False
    if a < 3 or a / max(1, a + l) < 0.6:
        return out, skipped
    pPr = p._p.pPr
    rtl = pPr.get("rtl") if pPr is not None else None
    algn = pPr.get("algn") if pPr is not None else None
    is_rtl = rtl in ("1", "true")
    if not is_rtl:
        out.append(("rtl", "rtl", "LTR", "RTL",
                    "الفقرة عربية واتجاهها من اليسار إلى اليمين. يُضبط اتجاهها RTL فتنتقل علامات الترقيم والأقواس إلى موضعها الصحيح عند العرض (لا تُعاد كتابة النص: الأقواس تنعكس بالعرض لا في الملف)",
                    "Arabic paragraph is LTR. Sets direction to RTL so punctuation and brackets land correctly when rendered (the text itself is not rewritten; brackets mirror at render time, not in the file)",
                    "high"))
    share_ok = a / max(1, a + l) >= 0.8
    group_mixed = mixed.get(key.split(".t")[0], False)
    if algn in ("l",) or (algn is None and not _is_placeholder(tf)):
        if share_ok and not group_mixed and (is_rtl or True):
            out.append(("align", "align", "يسار" if algn == "l" else "افتراضي (يسار)", "يمين",
                        "فقرة عربية محاذاتها إلى اليسار. تُضبط إلى اليمين مثل بقية النص العربي",
                        "Arabic paragraph is left-aligned. Sets it to right alignment",
                        "medium"))
        elif algn == "l":
            skipped = True
    return out, skipped


def _run_edits(text, para_ar, ntarget, tdom):
    """Edits for one run: list of (start, end, replacement, kind, note_ar, note_en, confidence)."""
    ed = []
    # words
    for m in re.finditer(r"\S+", text):
        tok = m.group(0)
        farsi = any(c in tok for c in engine.FARSI_LETTERS)
        if not AR.search(tok) and not (farsi and para_ar):
            continue
        # Persian letters
        if farsi:
            fixed = "".join(engine.FARSI_LETTERS.get(c, c) for c in tok)
            ed.append((m.start(), m.end(), fixed, "letters", "حرف فارسي داخل كلمة عربية", "Persian letter form inside an Arabic word", "high"))
            continue
        if TASH.search(tok):
            continue
        core = tok.strip(EDGE)
        if not core:
            continue
        off = tok.index(core)
        s0 = m.start() + off
        r = engine.spell_check_word(core)
        if r and "preferred spelling" not in r[2]:
            base, fixed, note = r
            if base == core:
                ed.append((s0, s0 + len(core), fixed, "spell", engine.note_ar(note), note, "high"))
                continue
        if tdom:
            w = tone._n(core)
            pre = ""
            table = tone.FRIENDLY if tdom == "formal" else tone.FORMAL
            key = w
            if key not in table and w[:1] in "وف" and w[1:] in table:
                pre, key = w[0], w[1:]
            rep = table.get(key)
            if key in table and rep and w == core:
                ed.append((s0, s0 + len(core), pre + rep, "tone", "كلمة تخالف النبرة السائدة (%s)" % ("رسمية" if tdom == "formal" else "ودّية"),
                           "Word breaks the dominant tone (%s)" % tdom, "medium" if tdom == "formal" else "low"))
    # digits (only in Arabic paragraphs)
    if para_ar and ntarget:
        for m in re.finditer(r"[0-9٠-٩۰-۹]+(?:[.,٫٬][0-9٠-٩۰-۹]+)*", text):
            a, b = m.start(), m.end()
            before = text[a - 1] if a else ""
            after = text[b] if b < len(text) else ""
            if LAT.match(before) or LAT.match(after):
                continue  # A4, 2FA, codes
            tok = m.group(0)
            fixed = _conv(tok, ntarget)
            if fixed != tok:
                ed.append((a, b, fixed, "digits", "توحيد الأرقام إلى النظام %s" % ("الهندي" if ntarget == "indic" else "الغربي"),
                           "Digits unified to %s" % ("Arabic-Indic" if ntarget == "indic" else "Western"), "high"))
        if ntarget == "indic":
            for m in re.finditer(r"(?<=[٠-٩])%", text):
                ed.append((m.start(), m.end(), "٪", "digits", "علامة النسبة العربية ٪", "Arabic percent sign", "high"))
    # punctuation (Arabic paragraphs only)
    if para_ar:
        for m in re.finditer(r"(?<=[\u0621-\u064A\u064B-\u065F])([,;?])(?=\s|$)", text):
            ar = {",": "،", ";": "؛", "?": "؟"}[m.group(1)]
            ed.append((m.start(), m.end(), ar, "punct", "علامة ترقيم لاتينية في نص عربي", "Latin punctuation in Arabic text", "high"))
        for m in re.finditer(r"(?<=\S)[ \t]{2,}(?=\S)", text):
            ed.append((m.start(), m.end(), " ", "punct", "مسافات مزدوجة", "Double space", "high"))
        for m in re.finditer(r"(?<=[\u0621-\u064A\u064B-\u065F])[ \t]+(?=[،؛؟])", text):
            ed.append((m.start(), m.end(), "", "punct", "مسافة قبل علامة الترقيم", "Space before punctuation", "high"))
    ed.sort(key=lambda e: (e[0], e[1]))
    # drop overlaps (keep first)
    out, last = [], -1
    for e in ed:
        if e[0] >= last:
            out.append(e); last = e[1]
    return out


def generate(path, brand):
    from pptx import Presentation
    prs = Presentation(path)
    ntarget, nwhy = _numeral_target(prs, getattr(brand, "numerals", "any"))
    tdom = _tone_dominant(prs)
    corr, skipped, align_skipped = [], 0, 0
    mixed = _group_mixed(prs)
    for si, name, key, tf in _frames(prs):
        for pi, p in enumerate(tf.paragraphs):
            ptext = "".join(r.text for r in p.runs)
            if not AR.search(ptext):
                continue
            para_ar = _para_is_ar(ptext)
            pe, sk = _para_edits(p, tf, key, mixed)
            align_skipped += sk
            for (suf, kind, bf, af, nar, nen, conf) in pe:
                ex = " ".join(ptext.split()[:7])
                corr.append(dict(id=f"{key}.{pi}.{suf}", kind=kind, slide=si, shape=name, before=bf, after=af,
                                 ctx_before=ex + " … ", ctx_after="", note_ar=nar, note_en=nen, confidence=conf,
                                 method="code: paragraph XML attribute", default_on=KINDS[kind][2]))
            for ri, r in enumerate(p.runs):
                if not AR.search(r.text) and not re.search(r"\d", r.text):
                    continue
                for (a, b, rep, kind, nar, nen, conf) in _run_edits(r.text, para_ar, ntarget, tdom):
                    ctx_a = " ".join(r.text[:a].split(" ")[-4:])
                    ctx_b = " ".join(r.text[b:].split(" ")[:4])
                    corr.append(dict(id=f"{key}.{pi}.{ri}.{a}", kind=kind, slide=si, shape=name, before=r.text[a:b], after=rep,
                                     ctx_before=ctx_a, ctx_after=ctx_b, note_ar=nar, note_en=nen, confidence=conf,
                                     method="code: " + {"spell": "curated spelling patterns", "letters": "character map", "digits": "digit map",
                                                        "punct": "regex", "tone": "word list"}[kind],
                                     default_on=KINDS[kind][2]))
            # words split over several runs cannot be matched safely
            if len(p.runs) > 1:
                joined = "".join(r.text for r in p.runs)
                if any(engine.spell_check_word(w) for w in re.findall(r"\S+", joined)):
                    per_run = set()
                    for r in p.runs:
                        for w in re.findall(r"\S+", r.text):
                            if engine.spell_check_word(w):
                                per_run.add(w)
                    for w in re.findall(r"\S+", joined):
                        if engine.spell_check_word(w) and w not in per_run:
                            skipped += 1
    return dict(corrections=corr, tone_dom=tdom, numeral_target=ntarget, numeral_why=nwhy, tone=tdom, skipped_split=skipped, align_skipped=align_skipped)


def apply(src, dst, selected_ids, ntarget, tdom):
    """Writes a corrected copy with only the chosen fix ids. ntarget/tdom come from generate() so ids stay stable."""
    from pptx import Presentation
    prs = Presentation(src)
    sel = set(selected_ids)
    applied = []
    mixed = _group_mixed(prs)
    for si, name, key, tf in _frames(prs):
        for pi, p in enumerate(tf.paragraphs):
            ptext = "".join(r.text for r in p.runs)
            if not AR.search(ptext):
                continue
            para_ar = _para_is_ar(ptext)
            for (suf, kind, *_r) in _para_edits(p, tf, key, mixed)[0]:
                cid = f"{key}.{pi}.{suf}"
                if cid in sel:
                    pPr = p._p.get_or_add_pPr()
                    if kind == "rtl":
                        pPr.set("rtl", "1")
                    else:
                        pPr.set("algn", "r")
                    applied.append(cid)
            for ri, r in enumerate(p.runs):
                if not AR.search(r.text) and not re.search(r"\d", r.text):
                    continue
                text, changed = r.text, False
                for (a, b, rep, kind, *_rest) in reversed(_run_edits(r.text, para_ar, ntarget, tdom)):
                    cid = f"{key}.{pi}.{ri}.{a}"
                    if cid in sel:
                        text = text[:a] + rep + text[b:]
                        changed = True
                        applied.append(cid)
                if changed:
                    r.text = text
    prs.save(dst)
    return applied
