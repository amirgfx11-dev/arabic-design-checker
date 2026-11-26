"""Arabic design checker engine (V1).

Everything in here is deterministic code: the same file in gives the same findings out.
No AI model is called. What the code can read is limited to:
  - PDF: the text layer (words, fonts, sizes, colors, positions) + rendered pixels
  - PPTX: text frames, runs, paragraph direction
  - PNG/JPG: pixels only (+ optional OCR for digits when tesseract is installed)
  - pasted text
"""
import io, os, re, shutil, subprocess, tempfile, unicodedata, hashlib
import tone as _tone
from collections import Counter, defaultdict

import fitz  # PyMuPDF
from PIL import Image

fitz.TOOLS.mupdf_display_errors(False)

# ----------------------------------------------------------------- constants
AR_LETTER = re.compile(r"[\u0621-\u064A\u066E-\u06D3\u0750-\u077F]")
LAT_LETTER = re.compile(r"[A-Za-z]")
INDIC = "٠١٢٣٤٥٦٧٨٩"
PERSIAN = "۰۱۲۳۴۵۶۷۸۹"
WEST = "0123456789"
TASHKEEL = re.compile(r"[\u064B-\u065F\u0670\u0640]")
BIDI_CTRL = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]")
RIGHT_JOIN_ONLY = set("اأإآدذرزوؤةءى") | set("ٱ")
FARSI_LETTERS = {"ی": "ي", "ک": "ك", "ە": "ه", "ۀ": "ة", "ھ": "ه", "ہ": "ه"}
CURRENCY = {"جنيه", "جنية", "ج.م", "ج", "EGP", "ريال", "درهم", "دينار", "ر.س", "د.إ", "SAR", "AED", "USD", "دولار", "$"}
RIGHT_ARROWS = set("→⇒➜➔➞➡▶►»›")
SENT_PUNCT = set(".،؟!:؛")

RULES = {
    "R1": ("Letter shaping / joining", "تشكيل الحروف وربطها"),
    "R2": ("Bidi / mixed-direction text", "اتجاه النص المختلط (عربي/إنجليزي)"),
    "R3": ("Numerals and prices", "الأرقام والأسعار"),
    "R4": ("Mirrored layout / alignment", "عكس التصميم والمحاذاة"),
    "R5": ("Spelling and typography", "الإملاء والتنسيق الكتابي"),
    "R6": ("Brand guidelines", "مطابقة دليل الهوية"),
    "R7": ("Page edges / layout", "حواف الصفحة والتخطيط"),
    "R9": ("Tone of voice (formal / friendly)", "نبرة الكتابة (رسمية / ودّية)"),
    "R8": ("Visual consistency (sizes, weights, alignment, spacing)", "اتساق التصميم (الأحجام والأوزان والمحاذاة والمسافات)"),
}


ARTIFACT = re.compile(r"[\u00bc-\u00be\u2044]|\d?\u2044\d")


def nfkc(s):
    # NFKC turns some Arabic ligature glyphs into text like "3/4": that is a text-layer artifact, not a real digit.
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[0-9]\u2044[0-9]", "", s)
    return BIDI_CTRL.sub("", s)


def has_ar(s):
    return bool(AR_LETTER.search(s))


def digit_kind(tok):
    k = set()
    for c in tok:
        if c in INDIC:
            k.add("indic")
        elif c in WEST:
            k.add("west")
        elif c in PERSIAN:
            k.add("persian")
    return k


# ----------------------------------------------------------------- findings
class Findings:
    def __init__(self):
        self.items = []
        self.pptx = {}
        self.tone = None
        self.stats = defaultdict(lambda: {"checked": 0, "flagged": 0, "note": ""})

    def checked(self, key, n=1):
        self.stats[key]["checked"] += n

    def note(self, key, text):
        self.stats[key]["note"] = text

    def add(self, key, rule, page, bbox, msg_ar, msg_en, severity="error", confidence="high",
            method="code", snippet="", loc=""):
        self.stats[key]["flagged"] += 1
        self.items.append(dict(check=key, rule=rule, page=page, bbox=[round(float(v), 1) for v in bbox] if bbox else None,
                               message_ar=msg_ar, message_en=msg_en, severity=severity,
                               confidence=confidence, method=method, snippet=snippet, loc=loc))


# ----------------------------------------------------------------- spelling (curated)
# (regex on the normalized word stem, replacement stem, note). Deliberately small and
# conservative: it only flags patterns that are wrong in standard Arabic spelling.
SPELL = [
    (r"^اسئل", "أسئل", "hamza on alef (أسئلة)"),
    (r"^مسؤل", "مسؤول", "missing waw (مسؤول)"),
    (r"^انشاء$", "إنشاء", "hamza under alef (إنشاء)"),
    (r"^ادار(ة|ي|ه)", "إدار", "hamza under alef (إدارة)"),
    (r"^اضاف", "إضاف", "hamza under alef (إضافة)"),
    (r"^ارسال", "إرسال", "hamza under alef (إرسال)"),
    (r"^اعاد(ة|ه)", "إعاد", "hamza under alef (إعادة)"),
    (r"^اجراء", "إجراء", "hamza under alef (إجراء)"),
    (r"^اجراءات", "إجراءات", "hamza under alef (إجراءات)"),
    (r"^اتمام", "إتمام", "hamza under alef (إتمام)"),
    (r"^انهاء", "إنهاء", "hamza under alef (إنهاء)"),
    (r"^اشعار", "إشعار", "hamza under alef (إشعار)"),
    (r"^امكان", "إمكان", "hamza under alef (إمكانية)"),
    (r"^اقام(ة|ه)", "إقام", "hamza under alef (إقامة)"),
    (r"^اثبات", "إثبات", "hamza under alef (إثبات)"),
    (r"^ابلاغ", "إبلاغ", "hamza under alef (إبلاغ)"),
    (r"^اكمال", "إكمال", "hamza under alef (إكمال)"),
    (r"^اذا$", "إذا", "hamza under alef (إذا)"),
    (r"^ايضا$", "أيضًا", "hamza on alef (أيضًا)"),
    (r"^تاكيد", "تأكيد", "hamza on alef (تأكيد)"),
    (r"^تاكد", "تأكد", "hamza on alef (تأكد)"),
    (r"^هاذ", "هذ", "extra alef (هذا / هذه)"),
    (r"^لاكن$", "لكن", "extra alef (لكن)"),
    (r"^مؤسسه$", "مؤسسة", "ta marbuta (مؤسسة)"),
    (r"^الان$", "الآن", "madda on alef (الآن)"),
    (r"^مسئولي", "مسؤولي", "preferred spelling (مسؤولية), accepted variant"),
]
SPELL_RE = [(re.compile(p), r, n) for p, r, n in SPELL]
PREFIXES = ["", "و", "ف", "ب", "ل", "ك", "ال", "وال", "بال", "لل", "فال", "كال"]


def norm_word(w):
    w = nfkc(w)
    w = TASHKEEL.sub("", w)
    return w.strip("،؛؟!.:()[]{}«»\"'“”-–—/\\|")


NOTE_AR = [("hamza on alef", "الهمزة على الألف"), ("hamza under alef", "الهمزة تحت الألف"), ("missing waw", "واو ناقصة"),
           ("extra alef", "ألف زائدة"), ("ta marbuta", "التاء المربوطة"), ("madda on alef", "المدة على الألف"),
           ("preferred spelling", "الرسم المفضّل، والآخر مقبول")]


def note_ar(note):
    for en, ar in NOTE_AR:
        if note.startswith(en):
            m = re.search(r"\(([^)]*)\)", note)
            return ar + (f": {m.group(1)}" if m and not note.startswith("preferred") else "")
    return note


def spell_check_word(w):
    """returns (matched stem, suggestion word, note) or None."""
    base = norm_word(w)
    if len(base) < 3 or not has_ar(base):
        return None
    for pre in PREFIXES:
        if not base.startswith(pre):
            continue
        stem = base[len(pre):]
        for rx, rep, note in SPELL_RE:
            m = rx.match(stem)
            if m:
                if stem.startswith(rep) or (note.startswith("preferred") and False):
                    continue
                fixed = pre + rep + stem[m.end():]
                if fixed != base:
                    return base, fixed, note
    return None


# ----------------------------------------------------------------- text-unit rules
class Token:
    __slots__ = ("text", "page", "bbox", "line_ar", "line_key", "loc")

    def __init__(self, text, page, bbox, line_ar, line_key, loc=""):
        self.text, self.page, self.bbox, self.line_ar, self.line_key, self.loc = text, page, bbox, line_ar, line_key, loc


def run_text_rules(F, tokens, numeral_pref="any", paste_mode=False, text_arrows=False):
    """Numerals, spelling, typography on a flat token list (works for every input type)."""
    F.note("R1", "Shaping is checked on PDF text layers (isolated-form letters inside words). Text typed or pasted as plain Unicode is shaped by the viewer, so there is nothing to check.")
    # ---------- numerals (R3)
    digit_toks = [t for t in tokens if digit_kind(t.text) and (t.line_ar or paste_mode)]
    cnt = Counter()
    for t in digit_toks:
        for k in digit_kind(t.text):
            cnt[k] += 1
    F.checked("R3", len(digit_toks))
    if numeral_pref == "indic":
        major = "indic"
    elif numeral_pref == "west":
        major = "west"
    elif cnt:
        major = "indic" if cnt["indic"] >= cnt["west"] else "west"
    else:
        major = None
    names = {"indic": ("هندية (٠١٢)", "Arabic-Indic (٠١٢)"), "west": ("غربية (012)", "Western (012)"), "persian": ("فارسية (۰۱۲)", "Persian (۰۱۲)")}
    if major and (len(cnt) > 1 or numeral_pref in ("indic", "west") or cnt.get("persian")):
        for t in digit_toks:
            kinds = digit_kind(t.text)
            bad = kinds - {major}
            if bad:
                b = sorted(bad)[0]
                why_ar = f"دليل الهوية يشترط الأرقام ال{names[major][0]}" if numeral_pref in ("indic", "west") else f"بقية المستند ({cnt[major]} رقم) تستخدم الأرقام ال{names[major][0]}"
                why_en = f"the brand rules require {names[major][1]}" if numeral_pref in ("indic", "west") else f"the rest of the document ({cnt[major]} numbers) uses {names[major][1]}"
                dg = "".join(re.findall(r"[0-9٠-٩۰-۹]+", t.text)) or t.text
                F.add("R3", "R3", t.page, t.bbox, f"الرقم «{dg}» مكتوب بالأرقام ال{names[b][0]}، بينما {why_ar}. يُفضَّل توحيد نظام الأرقام.",
                      f"Number \"{dg}\" uses {names[b][1]} but {why_en}. Use one numeral system.",
                      severity="error", confidence="high", method="code: digit script classification", snippet=t.text, loc=t.loc)
    # percent sign and currency position
    by_line = defaultdict(list)
    for t in tokens:
        by_line[(t.page, t.line_key)].append(t)
    for (pg, lk), toks in by_line.items():
        for t in toks:
            if any(c in INDIC for c in t.text) and "%" in t.text:
                F.checked("R3")
                F.add("R3", "R3", pg, t.bbox, f"«{t.text}»: أرقام هندية مع علامة النسبة اللاتينية (%). الأنسب استخدام ٪.",
                      f"\"{t.text}\": Arabic-Indic digits with Latin %. Prefer ٪.", severity="warn", confidence="high",
                      method="code: character classes", snippet=t.text, loc=t.loc)
        if not any(t.line_ar for t in toks) or any(t.bbox is None for t in toks):
            continue
        for i, t in enumerate(toks):
            if t.text.strip(".،:") in CURRENCY:
                nums = [u for u in toks if digit_kind(u.text) and u is not t]
                F.checked("R3")
                for u in nums:
                    # RTL reading: amount is read first, so the amount sits to the RIGHT of the currency.
                    if u.bbox and t.bbox and (t.bbox[0] + t.bbox[2]) / 2 > (u.bbox[0] + u.bbox[2]) / 2:
                        F.add("R3", "R3", pg, [min(t.bbox[0], u.bbox[0]), min(t.bbox[1], u.bbox[1]), max(t.bbox[2], u.bbox[2]), max(t.bbox[3], u.bbox[3])],
                              f"تظهر العملة «{t.text}» قبل المبلغ «{u.text}» عند القراءة. المعتاد في العربية كتابة المبلغ ثم العملة.",
                              f"Currency \"{t.text}\" is read before the amount \"{u.text}\". Usual Arabic order: amount, then currency.",
                              severity="warn", confidence="medium", method="code: visual position of tokens", snippet=t.text + " " + u.text, loc=t.loc)
                        break
    # ---------- spelling and typography (R5)
    prev = {}
    for t in tokens:
        if not has_ar(t.text) and not any(c in t.text for c in FARSI_LETTERS):
            continue
        F.checked("R5")
        s = spell_check_word(t.text)
        if s:
            base, fixed, note = s
            sev = "warn" if note.startswith(("preferred", "ta marbuta")) else "error"
            F.add("R5", "R5", t.page, t.bbox, f"خطأ إملائي: «{base}»، والصواب «{fixed}» ({note_ar(note)}).",
                  f"Spelling: \"{base}\" should be \"{fixed}\" ({note}).", severity=sev, confidence="high",
                  method="code: curated misspelling patterns (not a full dictionary)", snippet=base, loc=t.loc)
        farsi = [c for c in nfkc(t.text) if c in FARSI_LETTERS]
        if farsi and has_ar(t.text):
            fixed = "".join(FARSI_LETTERS.get(c, c) for c in nfkc(t.text))
            F.add("R5", "R5", t.page, t.bbox, f"تحتوي الكلمة «{nfkc(t.text)}» على حرف بشكل فارسي أو أردي ({'، '.join(farsi)}) داخل نص عربي. الصواب «{fixed}».",
                  f"Persian/Urdu letter form inside an Arabic word: \"{nfkc(t.text)}\" ({', '.join(farsi)}). Use \"{fixed}\".",
                  severity="error", confidence="high", method="code: Unicode letter check", snippet=nfkc(t.text), loc=t.loc)
        txt = nfkc(t.text)
        if re.search(r"[\u0621-\u064A][,;?]|[,;?][\u0621-\u064A]", txt) or re.fullmatch(r"[\u0621-\u064A]+[,;?]", txt):
            F.add("R5", "R5", t.page, t.bbox, f"استُخدمت علامة ترقيم لاتينية داخل نص عربي في «{txt}». الأنسب استخدام العلامات العربية: ، ؛ ؟",
                  f"Latin punctuation inside Arabic text in \"{txt}\". Use ، ؛ ؟", severity="warn", confidence="high",
                  method="code: character classes", snippet=txt, loc=t.loc)
    if text_arrows:
        for t in tokens:
            if t.line_ar and any(c in RIGHT_ARROWS for c in t.text):
                F.checked("R4")
                F.add("R4", "R4", t.page, t.bbox, f"السهم «{t.text}» يشير إلى اليمين داخل نص عربي. في التصاميم من اليمين إلى اليسار يشير سهم «التالي» إلى اليسار.",
                      f"Arrow \"{t.text}\" points right inside Arabic text. In RTL layouts \"next\" points left.", severity="warn", confidence="medium",
                      method="code: arrow character direction", snippet=t.text, loc=t.loc)
    # repeated word
    for (pg, lk), toks in by_line.items():
        for a, b in zip(toks, toks[1:]):
            na = norm_word(a.text)
            if len(na) >= 3 and has_ar(na) and na == norm_word(b.text):
                box = None
                if a.bbox and b.bbox:
                    box = [min(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1]), max(a.bbox[2], b.bbox[2]), max(a.bbox[3], b.bbox[3])]
                F.add("R5", "R5", pg, box, f"كلمة مكررة: «{na}».", f"Repeated word: \"{na}\".", severity="warn", confidence="medium",
                      method="code: adjacent duplicate tokens", snippet=na, loc=a.loc)

    try:
        _tone.check(F, tokens)
    except Exception:
        pass


def text_only_typography(F, text, page=None, loc=""):
    """Rules that need the raw string (spaces), so only for pasted / PPTX text."""
    F.checked("R5")
    if re.search(r"[^\S\n]{2,}", text):
        F.add("R5", "R5", page, None, "توجد مسافات مزدوجة في النص.", "Double spaces in the text.", severity="warn", method="code: regex", snippet=text[:40], loc=loc)
    if re.search(r"\s[،؛؟:.!]", text) and has_ar(text):
        F.add("R5", "R5", page, None, "توجد مسافة قبل علامة الترقيم.", "Space before a punctuation mark.", severity="warn", method="code: regex", snippet=text[:40], loc=loc)
    if re.search(r"[،؛؟][^\s\d)\]»”]", text):
        F.add("R5", "R5", page, None, "لا توجد مسافة بعد علامة الترقيم.", "Missing space after punctuation.", severity="warn", method="code: regex", snippet=text[:40], loc=loc)


# ----------------------------------------------------------------- brand rules
def font_family(name):
    n = re.sub(r"^[A-Z]{6}\+", "", name or "")
    n = re.split(r"[-,]", n)[0]
    return re.sub(r"[^a-z0-9\u0600-\u06ff]", "", n.lower())


class Brand:
    def __init__(self, fonts=None, colors=None, numerals="any", min_size=None, source_note=""):
        self.fonts = [f for f in (fonts or []) if f]
        self.colors = [c for c in (colors or []) if c]
        self.numerals = numerals if numerals in ("indic", "west") else "any"
        self.min_size = min_size
        self.source_note = source_note

    def active(self):
        return bool(self.fonts or self.colors or self.numerals != "any" or self.min_size)

    def summary(self):
        return dict(fonts=self.fonts, colors=self.colors, numerals=self.numerals, min_size=self.min_size, source_note=self.source_note)


def parse_hex_list(s):
    out = []
    for m in re.finditer(r"#?\b([0-9a-fA-F]{6})\b", s or ""):
        out.append("#" + m.group(1).lower())
    return list(dict.fromkeys(out))


def parse_guidelines_pdf(path):
    """Pull colors and fonts out of a brand-guidelines PDF. Returns a Brand (rules to be shown to the user)."""
    doc = fitz.open(path)
    text = "\n".join(p.get_text() for p in doc)
    colors = list(dict.fromkeys(["#" + m.group(1).lower() for m in re.finditer(r"#([0-9a-fA-F]{6})\b", text)]))
    embedded = Counter()
    for p in doc:
        for f in p.get_fonts():
            embedded[re.split(r"[-,]", re.sub(r"^[A-Z]{6}\+", "", f[3]))[0]] += 1
    emb_l = {n.lower().replace(" ", "") for n in embedded}
    # Words that follow "typography" in guideline headings but are not font names (this caused every font to read "Layout").
    stop = {"family", "size", "weight", "style", "layout", "grid", "colors", "colours", "color", "colour", "logo", "usage", "scale",
            "hierarchy", "spacing", "guidelines", "rules", "system", "overview", "headings", "heading", "body", "text", "print",
            "digital", "specimen", "palette", "imagery", "icons", "iconography", "tone", "voice", "principles", "intro", "introduction",
            "contents", "primary", "secondary", "regular", "bold", "light", "medium", "page", "section"}
    fonts = []
    for m in re.finditer(r"(?:font|typeface|typography|الخط|خط|الخطوط)[ \t]*[:：\-–][ \t]*([A-Za-z][A-Za-z0-9 ]{2,30})", text, re.I):
        f = m.group(1).strip()
        words = f.split()
        if not words or words[0].lower() in stop or f.lower() in stop:
            continue
        # keep only names that are plausible: appear embedded in the guide, or are short proper names
        if f.lower().replace(" ", "") in emb_l or any(w.lower().replace(" ", "") in emb_l for w in [f]) or len(words) <= 3:
            fonts.append(f)
    fonts = list(dict.fromkeys(fonts))
    if not fonts:
        generic = {"arial", "helvetica", "timesnewroman", "symbol", "zapfdingbats", "courier", "calibri"}
        fonts = [n for n, _ in embedded.most_common(6) if n.lower().replace(" ", "") not in generic][:3] or [n for n, _ in embedded.most_common(3)]
        note = "الخطوط مستنتجة من الخطوط المستخدمة داخل ملف الدليل نفسه"
    else:
        note = "الخطوط والألوان مقروءة من نص الدليل"
    numerals = "any"
    if re.search(r"أرقام هندية|الأرقام الهندية|Arabic-Indic|[٠١٢٣٤٥٦٧٨٩]{3}", text):
        numerals = "indic"
    elif re.search(r"أرقام غربية|الأرقام الغربية|Western numerals|Latin numerals", text):
        numerals = "west"
    return Brand(fonts[:6], colors[:12], numerals, None, "قُرئت القواعد تلقائيًا من ملف الدليل (%s). يُرجى مراجعتها قبل الاعتماد عليها." % note)


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def is_neutral(rgb):
    return max(rgb) - min(rgb) < 28


# ----------------------------------------------------------------- PDF
def tesseract_available():
    return bool(shutil.which("tesseract"))


def ocr_western_digits(pil_img):
    """Find Western-digit tokens in a raster image with tesseract (English). Returns [(text, (x0,y0,x1,y1) in px)].
    OCR is noisy on Arabic UI screenshots: only high-confidence pure-digit tokens are kept, and some digits will be missed."""
    if not tesseract_available():
        return []
    w, h = pil_img.size
    scale = 2 if max(w, h) < 1600 else 1
    im = pil_img.convert("RGB").resize((w * scale, h * scale))
    res = {}
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "i.png")
        im.save(p)
        for psm in ("11",):
            try:
                out = subprocess.run(["tesseract", p, "stdout", "--psm", psm, "-l", "eng", "tsv"],
                                     capture_output=True, text=True, timeout=40).stdout
            except Exception:
                continue
            for ln in out.splitlines()[1:]:
                c = ln.split("\t")
                if len(c) >= 12 and re.fullmatch(r"[0-9][0-9/.,:\-]*", c[11].strip() or "x") and float(c[10]) >= 85:
                    x, y, ww, hh = [int(v) / scale for v in c[6:10]]
                    key = (c[11].strip(), round(x / 30), round(y / 30))
                    res.setdefault(key, (c[11].strip(), (x, y, x + ww, y + hh)))
    return list(res.values())


def edge_strip_check(img):
    """Pixel check: a colored background that stops short of a page edge leaves a white strip.
    Flags an edge only when: the outermost rows are pure white, directly inside them is a uniform
    non-white color, and the same color runs to the opposite edge (so it is the page background,
    not a header band)."""
    img = img.convert("RGB")
    W, H = img.size
    px = img.load()

    def line(edge, k):
        if edge in ("bottom", "top"):
            y = H - 1 - k if edge == "bottom" else k
            vals = [px[x, y] for x in range(0, W, max(1, W // 100))]
        else:
            x = W - 1 - k if edge == "right" else k
            vals = [px[x, y] for y in range(0, H, max(1, H // 100))]
        n = len(vals)
        m = tuple(sum(v[i] for v in vals) / n for i in range(3))
        var = sum(sum((v[i] - m[i]) ** 2 for i in range(3)) for v in vals) / n
        return m, var

    opposite = {"bottom": "top", "top": "bottom", "left": "right", "right": "left"}
    dim = {"bottom": H, "top": H, "left": W, "right": W}
    found = []
    for edge in ("bottom", "top", "left", "right"):
        m0, v0 = line(edge, 0)
        if not (min(m0) > 245 and v0 < 4):
            continue
        t = 0
        while t < dim[edge] * 0.15 and min(line(edge, t)[0]) > 245 and line(edge, t)[1] < 4:
            t += 1
        if t < 2 or t >= dim[edge] * 0.15:
            continue
        mi, vi = line(edge, t + 2)
        if max(mi) >= 232 or vi >= 60 or is_neutral(tuple(int(v) for v in mi)):
            continue
        mo, vo = line(opposite[edge], 0)
        if rgb_dist(mo, mi) < 25 and vo < 60:
            found.append((edge, t))
    return found



def bracket_shape(page, bbox):
    """-1 if the drawn glyph looks like '(' (ink weighted to the left), +1 if like ')', 0 if unclear."""
    try:
        r = fitz.Rect(bbox)
        pix = page.get_pixmap(dpi=200, clip=r)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    except Exception:
        return 0
    w, h = img.size
    if w < 4 or h < 4:
        return 0
    px = img.load()
    bg = sorted(px[x, y] for x in (0, w - 1) for y in (0, h - 1))[1]
    tot = cx = 0.0
    for y in range(h):
        for x in range(w):
            v = abs(px[x, y] - bg)
            if v > 40:
                tot += v
                cx += v * x
    if tot < 50:
        return 0
    # centroid of the ink; for a bracket the bulge side holds the extreme of the stroke: use ink extremes at mid-height vs ends
    cols = [sum(1 for y in range(h) if abs(px[x, y] - bg) > 40) for x in range(w)]
    xs = [x for x, c in enumerate(cols) if c]
    if not xs:
        return 0
    # '(' has its tallest-stroke column near the left; ')' near the right
    mid = (xs[0] + xs[-1]) / 2
    peak = max(range(xs[0], xs[-1] + 1), key=lambda x: cols[x])
    off = (peak - mid) / max(1, (xs[-1] - xs[0]))
    # vertical extent at the edges tells which way it opens: compare ink rows at the left and right extreme columns
    left_ext = sum(1 for y in range(h) if abs(px[xs[0], y] - bg) > 40)
    right_ext = sum(1 for y in range(h) if abs(px[xs[-1], y] - bg) > 40)
    # '(' : leftmost column is the bulge (spans mid rows, short), rightmost column are the tips (top and bottom). Use centroid as the main signal.
    c = cx / tot
    rel = (c - (w - 1) / 2) / w
    if abs(rel) < 0.03:
        return 0
    return -1 if rel < 0 else 1


def analyze_pdf(path, F, brand, max_pages=60, ocr=True, dpi=40):
    doc = fitz.open(path)
    n_pages = min(len(doc), max_pages)
    tokens = []
    font_use = defaultdict(list)    # family -> [(page, bbox)]
    size_bad = []
    color_bad = defaultdict(list)
    raw_pages = []
    page_info = []
    for pi in range(n_pages):
        page = doc[pi]
        W, H = page.rect.width, page.rect.height
        page_info.append((W, H))
        words = []
        d = page.get_text("rawdict")
        blocks_geo = []
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            blk_lines = []
            for ln in b["lines"]:
                chars = []
                for sp in ln["spans"]:
                    fam = font_family(sp["font"])
                    sz = sp["size"]
                    col = sp["color"]
                    rgb = ((col >> 16) & 255, (col >> 8) & 255, col & 255)
                    txt = "".join(c["c"] for c in sp["chars"]).strip()
                    if txt and re.search(r"[A-Za-z\u0621-\u064A]", nfkc(txt)):
                        font_use[fam].append((pi + 1, sp["bbox"], sp["font"]))
                        if brand.min_size and sz < brand.min_size - 0.01:
                            size_bad.append((pi + 1, sp["bbox"], sz, txt))
                        if brand.colors and not is_neutral(rgb):
                            if min(rgb_dist(rgb, hex_to_rgb(c)) for c in brand.colors) > 40:
                                color_bad[rgb].append((pi + 1, sp["bbox"], txt))
                    for c in sp["chars"]:
                        chars.append((c["c"], c["bbox"], sp["font"]))
                if chars:
                    blk_lines.append((ln["bbox"], chars))
            blocks_geo.append((b["bbox"], blk_lines))
            for li, (lb, chs) in enumerate(blk_lines):
                cur = []
                def flush():
                    if cur:
                        raw = "".join(c for c, _, _ in cur)
                        bb = [min(b_[0] for _, b_, _ in cur), min(b_[1] for _, b_, _ in cur), max(b_[2] for _, b_, _ in cur), max(b_[3] for _, b_, _ in cur)]
                        words.append((raw, bb, (len(blocks_geo), li)))
                        cur.clear()
                for ch in chs:
                    if ch[0].isspace():
                        flush()
                    else:
                        cur.append(ch)
                flush()
        line_ar = defaultdict(bool)
        for raw, bb, lk in words:
            if has_ar(nfkc(raw)):
                line_ar[lk] = True
        for raw, bb, lk in words:
            tokens.append(Token(nfkc(raw), pi + 1, bb, line_ar[lk], lk))
        raw_pages.append(blocks_geo)
        # ---- R1 shaping: isolated forms followed by another letter in the same word
        for w in words:
            raw = w[0]
            F.checked("R1")
            bad = 0
            letters = [c for c in raw if AR_LETTER.search(unicodedata.normalize("NFKC", c)) or "\uFB50" <= c <= "\uFEFC"]
            for i, c in enumerate(raw[:-1]):
                dec = unicodedata.decomposition(c)
                if dec.startswith("<isolated>") and ("\uFB50" <= raw[i + 1] <= "\uFEFC" or AR_LETTER.search(raw[i + 1])):
                    base = unicodedata.normalize("NFKC", c)
                    if len(base) == 1 and base not in RIGHT_JOIN_ONLY and base != "ء":
                        bad += 1
            if bad:
                F.add("R1", "R1", pi + 1, w[1], f"حروف غير متصلة في «{nfkc(raw)}»: {bad} حرف مرسوم بالشكل المنفصل مع أن العربية تتطلب وصله بالحرف التالي.",
                      f"Disconnected letters in \"{nfkc(raw)}\": {bad} letters drawn in isolated form where Arabic requires joining.",
                      severity="error", confidence="high", method="code: Unicode joining forms in the text layer", snippet=nfkc(raw))
        # ---- R2 bidi geometry + R4 arrows / punctuation
        for bbox, lines in blocks_geo:
            merged = []
            for lb, chars in sorted(lines, key=lambda l: (l[0][1] + l[0][3]) / 2):
                yc = (lb[1] + lb[3]) / 2
                if merged and abs(merged[-1][2] - yc) < 0.35 * (lb[3] - lb[1]):
                    merged[-1][1].extend(chars)
                    merged[-1][0] = [min(merged[-1][0][0], lb[0]), min(merged[-1][0][1], lb[1]), max(merged[-1][0][2], lb[2]), max(merged[-1][0][3], lb[3])]
                else:
                    merged.append([list(lb), list(chars), yc])
            for lb, chars, _y in merged:
                vis = [(c, bb) for c, bb, f in chars if c.strip()]
                if len(vis) < 3:
                    continue
                n_ar = sum(1 for c, _ in vis if AR_LETTER.search(nfkc(c)))
                n_la = sum(1 for c, _ in vis if LAT_LETTER.search(c))
                if n_ar == 0:
                    continue
                F.checked("R2")
                # brackets: judged from the DRAWN SHAPE (pixels), not the character code, because PDF exporters
                # differ in which code they store for a mirrored bracket. Correct in an RTL line: the left glyph
                # looks like "(" (ink on its left) and the right glyph looks like ")" (ink on its right).
                brs = sorted([(bb, c) for c, bb in vis if c in "()[]"], key=lambda t: t[0][0])
                for (b1, c1), (b2, c2) in zip(brs, brs[1:]):
                    if {c1, c2} - set("()[]") or (c1 in "([") == (c2 in "([") and False:
                        continue
                    between = [bb for c, bb in vis if AR_LETTER.search(nfkc(c)) and b1[2] <= bb[0] and bb[2] <= b2[0] + 0.5]
                    if not between:
                        continue
                    s1, s2 = bracket_shape(page, b1), bracket_shape(page, b2)
                    if s1 == 1 and s2 == -1:
                        F.add("R2", "R2", pi + 1, lb, "الأقواس غير معكوسة بشكل صحيح حول النص العربي: القوس الأيسر مرسوم \")\" والأيمن \"(\"، فيتجهان بعيدًا عن النص بدل أن يحيطا به.",
                              "Brackets are not mirrored around Arabic text: the left glyph is drawn as \")\" and the right one as \"(\", so they face away from the content.",
                              severity="error", confidence="high", method="code: bracket glyph shape measured from pixels", snippet="".join(c for c, _ in vis)[:30])
                        break
                # Latin run printed backwards
                run = []
                runs = []
                for c, bb in vis:
                    if LAT_LETTER.search(c):
                        run.append((c, bb))
                    else:
                        if len(run) >= 3:
                            runs.append(run)
                        run = []
                if len(run) >= 3:
                    runs.append(run)
                for r in runs:
                    xs = [bb[0] for _, bb in r]
                    if all(a > b for a, b in zip(xs, xs[1:])):
                        txt = "".join(c for c, _ in r)
                        F.add("R2", "R2", pi + 1, [min(bb[0] for _, bb in r), min(bb[1] for _, bb in r), max(bb[2] for _, bb in r), max(bb[3] for _, bb in r)],
                              f"كلمة لاتينية معكوسة داخل النص العربي: «{txt}» مرسومة من اليمين إلى اليسار.",
                              f"Latin text drawn backwards inside Arabic text: \"{txt}\" runs right to left.", severity="error", confidence="high",
                              method="code: character x-positions", snippet=txt)
                # sentence punctuation at the wrong end
                if n_ar >= 4 and n_ar > n_la:
                    rightmost = max(vis, key=lambda cb: cb[1][2])
                    if rightmost[0] in SENT_PUNCT and rightmost[0] != ":" and len(vis) >= 6:
                        F.add("R2", "R2", pi + 1, rightmost[1], f"تظهر علامة الترقيم «{rightmost[0]}» في بداية السطر (أقصى اليمين) بدل نهاية الجملة. يحدث ذلك عادةً عندما يوضع النص العربي في فقرة اتجاهها من اليسار إلى اليمين.",
                              f"Punctuation mark \"{rightmost[0]}\" sits at the start (far right) of an Arabic line instead of the end. Typical sign of a left-to-right paragraph.",
                              severity="error", confidence="medium", method="code: character x-positions", snippet=rightmost[0])
        # ---- R4 alignment of long Arabic paragraphs
        longb = []
        for bbox, lines in blocks_geo:
            txt = "".join(c for lb, ch in lines for c, _, _ in ch)
            if len(lines) >= 1 and sum(1 for c in txt if AR_LETTER.search(nfkc(c))) >= 40:
                longb.append(bbox)
        if len(longb) >= 3:
            F.checked("R4", len(longb))
            rights = Counter(round(b[2] / 3) for b in longb)
            mode_r, cnt_r = rights.most_common(1)[0]
            if cnt_r >= 2:
                for b in longb:
                    if abs(b[2] - mode_r * 3) > 12 and abs(b[0] - min(x[0] for x in longb)) < 4 and (mode_r * 3 - b[2]) > 0.18 * W:
                        F.add("R4", "R4", pi + 1, b, "فقرة عربية محاذاتها إلى اليسار، بينما بقية الفقرات العربية في الصفحة محاذاة إلى اليمين.",
                              "Arabic paragraph is aligned to the left while the other Arabic paragraphs on the page are aligned right.",
                              severity="warn", confidence="medium", method="code: block edge positions")
    F.note("R4", "Arrow and icon direction cannot be read from a PDF text layer (a font can draw a left arrow from a right-arrow character), so it is not tested here. It needs the vision step planned for a later version. Right-edge alignment of long Arabic paragraphs is tested.")
    F.note("R2", "For PDFs the code checks what can be proven from the page geometry: bracket mirroring, reversed Latin text, punctuation at the wrong end. It cannot know the intended word order of a flattened design.")
    # ---- run text rules
    run_text_rules(F, tokens, brand.numerals)
    # ---- R6 brand
    if brand.active():
        if brand.fonts:
            allowed = [font_family(f) for f in brand.fonts]
            for fam, uses in font_use.items():
                F.checked("R6", len(uses))
                if not any(a and (a in fam or fam in a) for a in allowed):
                    pg, bb, fname = uses[0]
                    F.add("R6", "R6", pg, bb, f"الخط «{re.sub(r'^[A-Z]{6}[+]', '', fname)}» ليس ضمن خطوط الهوية ({'، '.join(brand.fonts)}). استُخدم {len(uses)} مرة، وأولها في الصفحة {pg}.",
                          f"Font \"{re.sub(r'^[A-Z]{6}[+]', '', fname)}\" is not one of the brand fonts ({', '.join(brand.fonts)}). Used {len(uses)} times, first on page {pg}.",
                          severity="error", confidence="high", method="code: font names in the PDF", snippet=fname)
                    for pg2, bb2, _ in uses[1:3]:
                        F.items[-1].setdefault("extra", []).append([pg2, [round(v, 1) for v in bb2]])
        if brand.min_size:
            F.checked("R6", len(size_bad) + 1)
            for pg, bb, sz, txt in size_bad[:20]:
                F.add("R6", "R6", pg, bb, f"حجم الخط {sz:.1f}pt أصغر من الحد الأدنى في دليل الهوية ({brand.min_size}pt): «{nfkc(txt)[:30]}».",
                      f"Text size {sz:.1f}pt is below the guideline minimum ({brand.min_size}pt): \"{nfkc(txt)[:30]}\".", severity="warn",
                      confidence="high", method="code: font sizes in the PDF", snippet=nfkc(txt)[:30])
        if brand.colors:
            for rgb, uses in color_bad.items():
                F.checked("R6", len(uses))
                pg, bb, txt = uses[0]
                hx = "#%02x%02x%02x" % rgb
                F.add("R6", "R6", pg, bb, f"لون النص {hx} ليس من ألوان الهوية ({'، '.join(brand.colors)}). استُخدم {len(uses)} مرة، وأولها في الصفحة {pg}.",
                      f"Text color {hx} is not in the brand palette ({', '.join(brand.colors)}). Used {len(uses)} times, first on page {pg}.",
                      severity="warn", confidence="high", method="code: text colors in the PDF", snippet=hx)
    # ---- render pages once: edge check + OCR of raster digits
    return doc, n_pages, page_info


def render_page(doc, pno, dpi=110):
    pix = doc[pno].get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def pixel_checks_pdf(doc, n_pages, F, brand, ocr=True):
    major_indic = None
    # decide numeral majority for OCR comparison from findings stats
    txt_digits = Counter()
    for pi in range(n_pages):
        for w in doc[pi].get_text("words"):
            for k in digit_kind(nfkc(w[4])):
                txt_digits[k] += 1
    if brand.numerals != "any":
        major_indic = brand.numerals == "indic"
    elif txt_digits:
        major_indic = txt_digits["indic"] > txt_digits["west"]
    import time as _t
    deadline = _t.time() + 70
    for pi in range(n_pages):
        page = doc[pi]
        img = render_page(doc, pi, 40)
        F.checked("R7")
        for edge, t in edge_strip_check(img):
            W, H = page.rect.width, page.rect.height
            thick = t * (72 / 40)
            box = {"bottom": [0, H - thick, W, H], "top": [0, 0, W, thick], "left": [0, 0, thick, H], "right": [W - thick, 0, W, H]}[edge]
            F.add("R7", "R7", pi + 1, box, f"الخلفية لا تغطي حافة الصفحة ({ {'bottom':'السفلية','top':'العلوية','left':'اليسرى','right':'اليمنى'}[edge] }): يتبقى شريط أبيض بسمك {thick:.0f}pt تقريبًا.",
                  f"The background does not reach the {edge} edge of the page: a white strip about {thick:.0f}pt thick remains.",
                  severity="warn", confidence="medium", method="code: pixel scan of the page edges", snippet=edge)
        if ocr and major_indic and tesseract_available() and _t.time() < deadline:
            for im_info in page.get_images(full=True):
                xref = im_info[0]
                try:
                    pm = fitz.Pixmap(doc, xref)
                    if pm.alpha or pm.colorspace is None or pm.colorspace.n != 3:
                        pm = fitz.Pixmap(fitz.csRGB, pm)
                except Exception:
                    continue
                if pm.width < 600 or pm.height < 300:
                    continue
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                r = rects[0]
                pil = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                hits = ocr_western_digits(pil)
                F.checked("R3", len(hits))
                for text, (x0, y0, x1, y1) in hits:
                    sx, sy = r.width / pm.width, r.height / pm.height
                    bb = [r.x0 + x0 * sx - 3, r.y0 + y0 * sy - 3, r.x0 + x1 * sx + 3, r.y0 + y1 * sy + 3]
                    F.add("R3", "R3", pi + 1, bb, f"داخل صورة أو لقطة شاشة: الرقم «{text}» مكتوب بالأرقام الغربية، بينما بقية المستند يستخدم الأرقام الهندية. (قُرئ بتقنية OCR، يُرجى التأكد بالعين)",
                          f"Inside a raster image: \"{text}\" is in Western digits while the document text uses Arabic-Indic digits. (Read by OCR, please confirm by eye.)",
                          severity="warn", confidence="medium", method="code + OCR (tesseract) on embedded images", snippet=text)
    if not tesseract_available():
        F.note("R3", "Digits inside embedded screenshots were not checked here because OCR (tesseract) is not installed on this server.")
    else:
        F.note("R3", "Text-layer digits are checked exactly. Digits inside embedded screenshots are read by OCR, so those results are medium confidence.")
    return


# ----------------------------------------------------------------- R8 visual consistency (PDF text layer geometry)
def pptx_render_doc(src, pptx_layout, workdir):
    """Real render of the slides through LibreOffice (headless). Returns a fitz doc, or None if it cannot be trusted
    (no LibreOffice, timeout, page count or page size not matching the slide layout)."""
    import subprocess, shutil as _sh, os
    exe = _sh.which("soffice") or _sh.which("libreoffice")
    if not exe or not pptx_layout:
        return None
    try:
        out = os.path.join(workdir, "lo_out"); os.makedirs(out, exist_ok=True)
        prof = os.path.join(workdir, "lo_profile")
        subprocess.run([exe, "--headless", "--norestore", "-env:UserInstallation=file://" + prof,
                        "--convert-to", "pdf", "--outdir", out, src],
                       check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdfs = [f for f in os.listdir(out) if f.lower().endswith(".pdf")]
        if not pdfs:
            return None
        d = fitz.open(os.path.join(out, pdfs[0]))
        order = sorted(pptx_layout)
        if d.page_count != len(order) or order != list(range(order[0], order[0] + len(order))):
            return None
        for i, si in enumerate(order):
            W, H, _ = pptx_layout[si]
            r = d[i].rect
            if abs(r.width - W) > 0.03 * W or abs(r.height - H) > 0.03 * H:
                return None
        return d
    except Exception:
        return None


def pptx_wireframe_doc(pptx_layout):
    """PPTX has no renderer here, so the report shows a schematic: gray boxes where text frames sit."""
    d = fitz.open()
    for si in sorted(pptx_layout):
        W, H, boxes = pptx_layout[si]
        pg = d.new_page(width=max(W, 100), height=max(H, 100))
        pg.draw_rect(pg.rect, color=(0.8, 0.8, 0.8), fill=(0.98, 0.98, 0.98))
        for bb in boxes:
            pg.draw_rect(fitz.Rect(*bb), color=(0.55, 0.6, 0.65), fill=(0.9, 0.93, 0.95), width=0.8)
        pg.insert_text((8, 14), "Schematic of slide %d (text frames only, not a render)" % si, fontsize=9, color=(0.4, 0.4, 0.4))
    return d


def _is_bold(sp):
    return bool(sp["flags"] & 16) or bool(re.search(r"bold|black|heavy|semibold|demi", sp["font"], re.I))


def _page_blocks(page):
    """Paragraph-like blocks: dominant size, family, weight, geometry. Skips tiny/mixed blocks."""
    out = []
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        sizes, fams, bolds, chars = Counter(), Counter(), Counter(), 0
        for ln in b["lines"]:
            for sp in ln["spans"]:
                t = sp["text"].strip()
                if not t:
                    continue
                n = len(t)
                chars += n
                sizes[round(sp["size"] * 2) / 2.0] += n
                fams[font_family(sp["font"])] += n
                bolds[_is_bold(sp)] += n
        if not chars or not sizes:
            continue
        size, sn = sizes.most_common(1)[0]
        if sn < 0.8 * chars:      # mixed sizes in one block: a heading with inline text, skip
            continue
        bb = b["bbox"]
        out.append(dict(bbox=list(bb), size=size, fam=fams.most_common(1)[0][0], bold=bolds.most_common(1)[0][0],
                        chars=chars, lines=len(b["lines"]), w=bb[2] - bb[0], h=bb[3] - bb[1]))
    return out


def _hover(a, b):
    ov = min(a[2], b[2]) - max(a[0], b[0])
    return ov / max(1.0, min(a[2] - a[0], b[2] - b[0]))


def _union(*bbs):
    return [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]


def _stacks(blocks):
    """Chains of vertically stacked blocks of the same style (lists, card titles, repeated labels)."""
    bl = sorted(blocks, key=lambda b: b["bbox"][1])
    used, chains = set(), []
    for i, a in enumerate(bl):
        if i in used:
            continue
        chain, cur = [a], a
        used.add(i)
        while True:
            nxt = None
            for j, c in enumerate(bl):
                if j in used or c["bbox"][1] < cur["bbox"][3] - 1:
                    continue
                gap = c["bbox"][1] - cur["bbox"][3]
                if gap > 3 * cur["size"] + 24:
                    break
                if abs(c["size"] - cur["size"]) <= 0.3 and c["fam"] == cur["fam"] and _hover(cur["bbox"], c["bbox"]) > 0.5:
                    nxt = (j, c)
                    break
            if not nxt:
                break
            used.add(nxt[0])
            chain.append(nxt[1])
            cur = nxt[1]
        if len(chain) >= 3:
            chains.append(chain)
    return chains


def consistency_pdf(doc, n_pages, F):
    key = "R8"
    pages = []
    for pi in range(n_pages):
        try:
            pages.append(_page_blocks(doc[pi]))
        except Exception:
            pages.append([])
    allb = [b for bl in pages for b in bl]
    F.checked(key, len(allb))
    # ---- (a) font-size outlier among body paragraphs
    body = [b for b in allb if b["chars"] >= 40 and (b["lines"] >= 2 or b["chars"] >= 60)]
    wsize = Counter()
    for b in body:
        wsize[b["size"]] += b["chars"]
    if wsize:
        s0, _ = wsize.most_common(1)[0]
        tot = sum(wsize.values())
        for pi, bl in enumerate(pages):
            bb_ = [b for b in bl if b in body]
            for b in bb_:
                diff = b["size"] - s0
                if abs(diff) < max(0.8, 0.07 * s0) or abs(diff) > 0.3 * s0:
                    continue
                if sum(1 for q in body if q["size"] == b["size"]) > max(2, 0.1 * len(body)):
                    continue        # the size is used by many paragraphs: it is a real style, not a slip
                same_style = [n for n in bb_ if n is not b and abs(n["size"] - s0) <= 0.3 and n["fam"] == b["fam"] and n["bold"] == b["bold"]]
                near = [n for n in same_style if _hover(b["bbox"], n["bbox"]) > 0.5 and
                        (0 <= n["bbox"][1] - b["bbox"][3] <= 40 or 0 <= b["bbox"][1] - n["bbox"][3] <= 40)]
                if not near:
                    continue
                n = near[0]
                rel = "أصغر" if diff < 0 else "أكبر"
                rel_en = "smaller" if diff < 0 else "larger"
                after = n["bbox"][1] > b["bbox"][1]
                F.add(key, "R8", pi + 1, b["bbox"],
                      f"حجم خط الفقرة {b['size']:g}pt، وهو {rel} من الفقرة {'التي تليها' if after else 'التي تسبقها'} ({n['size']:g}pt) بالخط والوزن نفسيهما، دون سبب ظاهر (فهي ليست عنوانًا ولا تعليقًا).",
                      f"Paragraph set at {b['size']:g}pt is {rel_en} than the neighbouring paragraph ({n['size']:g}pt) in the same font and weight, and {b['size']:g}pt is rare in this document (a one-off, not a style).",
                      severity="warn", confidence="medium", method="code: font sizes from the PDF text layer", snippet=f"{b['size']:g}pt vs {n['size']:g}pt",
                      )
                F.items[-1]["extra"] = [[pi + 1, [round(v, 1) for v in n["bbox"]]]]
    # ---- (b)(c)(d) stacks of similar elements
    for pi, bl in enumerate(pages):
        for ch in _stacks(bl):
            n = len(ch)
            # (b) weight
            if n >= 4:
                bolds = Counter(b["bold"] for b in ch)
                if len(bolds) == 2 and min(bolds.values()) == 1:
                    odd = [b for b in ch if bolds[b["bold"]] == 1][0]
                    # a lone bold first item is a header, not a slip: skip the first element
                    if odd is not ch[0]:
                        F.add(key, "R8", pi + 1, odd["bbox"],
                              f"عنصر من بين {n} عناصر متشابهة (الخط والحجم نفسهما) وزنه مختلف: {'عريض' if odd['bold'] else 'عادي'} بينما بقية العناصر {'عادية' if odd['bold'] else 'عريضة'}.",
                              f"One of {n} otherwise identical stacked elements (same font and size) has a different weight: {'bold' if odd['bold'] else 'regular'} while the rest are {'regular' if odd['bold'] else 'bold'}.",
                              severity="warn", confidence="low", method="code: font names and sizes from the PDF text layer", snippet="weight")
            # (c) alignment edge
            cen = [(b["bbox"][0] + b["bbox"][2]) / 2 for b in ch]
            cmed = sorted(cen)[n // 2]
            centered = sum(1 for v in cen if abs(v - cmed) <= 3.0) >= n - 1   # centered labels: edges differ by design
            if n >= 4 and not centered:
                for edge, idx in (("right", 2), ("left", 0)):
                    vals = [b["bbox"][idx] for b in ch]
                    med = sorted(vals)[len(vals) // 2]
                    ok = [v for v in vals if abs(v - med) <= 2.0]
                    if len(ok) == n - 1 or (n >= 6 and len(ok) == n - 1):
                        odd = [b for b in ch if abs(b["bbox"][idx] - med) > 2.0][0]
                        dv = abs(odd["bbox"][idx] - med)
                        if 6 <= dv <= 60 and all(b["lines"] == 1 for b in ch):
                            F.add(key, "R8", pi + 1, odd["bbox"],
                                  f"عنصر من بين {n} عناصر متشابهة تختلف حافته {'اليمنى' if edge == 'right' else 'اليسرى'} عن خط المحاذاة المشترك بنحو {dv:.0f}pt.",
                                  f"One of {n} similar stacked elements is off the shared {edge} edge by about {dv:.0f}pt.",
                                  severity="warn", confidence="low", method="code: text box positions from the PDF", snippet=f"{edge} edge {dv:.0f}pt")
                            break
            # (d) spacing between repeated elements
            if n >= 4:
                gaps = [ch[i + 1]["bbox"][1] - ch[i]["bbox"][3] for i in range(n - 1)]
                for gi, g in enumerate(gaps):
                    others = gaps[:gi] + gaps[gi + 1:]
                    m = sorted(others)[len(others) // 2]
                    if m >= 3 and all(abs(o - m) <= max(1.5, 0.2 * m) for o in others) and abs(g - m) >= max(6.0, 0.5 * m):
                        a, b = ch[gi], ch[gi + 1]
                        F.add(key, "R8", pi + 1, _union(a["bbox"], b["bbox"]),
                              f"المسافة بين عنصرين متكررين ({g:.0f}pt) تختلف عن بقية المسافات في المجموعة نفسها (نحو {m:.0f}pt).",
                              f"The gap between two repeated elements ({g:.0f}pt) differs from the other gaps in the same group (about {m:.0f}pt).",
                              severity="warn", confidence="medium" if len(others) >= 3 else "low",
                              method="code: text box positions from the PDF", snippet=f"gap {g:.0f} vs {m:.0f}")
                        break
    F.note(key, "Uses font sizes, weights and box positions from the PDF text layer. It cannot see images, shapes or intent, so a deliberate emphasis can look like an error. Medium means the pattern is clear in the geometry, low means check by eye.")


# ----------------------------------------------------------------- PPTX
def analyze_pptx(path, F, brand):
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation(path)
    tokens = []
    font_use = defaultdict(list)
    n_par = 0
    SW, SH = prs.slide_width / 12700.0, prs.slide_height / 12700.0
    for si, slide in enumerate(prs.slides, 1):
        F.pptx[si] = (SW, SH, [])
        def walk(shapes):
            for sh in shapes:
                if sh.shape_type == 6:  # group
                    yield from walk(sh.shapes)
                else:
                    yield sh
        for sh in walk(slide.shapes):
            try:
                sbb = [sh.left / 12700.0, sh.top / 12700.0, (sh.left + sh.width) / 12700.0, (sh.top + sh.height) / 12700.0]
                if sh.has_text_frame and sh.text_frame.text.strip():
                    F.pptx[si][2].append(sbb)
            except Exception:
                sbb = None
            frames = []
            if sh.has_text_frame:
                frames.append(sh.text_frame)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    for cell in row.cells:
                        frames.append(cell.text_frame)
            for tf in frames:
                for pi, para in enumerate(tf.paragraphs):
                    text = "".join(r.text for r in para.runs)
                    if not text.strip():
                        continue
                    loc = f"slide {si}, {sh.name}"
                    n_par += 1
                    ar_n = len(AR_LETTER.findall(nfkc(text)))
                    la_n = len(LAT_LETTER.findall(text))
                    pPr = para._p.pPr
                    rtl = pPr.get("rtl") if pPr is not None else None
                    algn = pPr.get("algn") if pPr is not None else None
                    if ar_n > la_n and ar_n >= 3:
                        F.checked("R2")
                        if rtl not in ("1", "true"):
                            F.add("R2", "R2", si, sbb, f"فقرة عربية اتجاهها من اليسار إلى اليمين (لم يُفعَّل RTL): «{text[:40]}». النتيجة: تنتقل علامات الترقيم والأرقام والكلمات اللاتينية إلى موضع خاطئ.",
                                  f"Arabic paragraph is left-to-right (rtl not set): \"{text[:40]}\". Punctuation, numbers and Latin words will land on the wrong side.",
                                  severity="error", confidence="high", method="code: paragraph rtl attribute", snippet=text[:40], loc=loc)
                        F.checked("R4")
                        if algn == "l" and rtl in ("1", "true"):
                            F.add("R4", "R4", si, sbb, f"فقرة عربية اتجاهها RTL لكن محاذاتها إلى اليسار: «{text[:40]}».",
                                  f"RTL Arabic paragraph is aligned left: \"{text[:40]}\".", severity="warn", confidence="medium",
                                  method="code: paragraph alignment attribute", snippet=text[:40], loc=loc)
                    for r in para.runs:
                        if r.text.strip():
                            fn = r.font.name or (tf.paragraphs[0].runs[0].font.name if tf.paragraphs[0].runs else None)
                            if fn:
                                font_use[font_family(fn)].append((si, loc, fn))
                            if brand.min_size and r.font.size and r.font.size.pt < brand.min_size:
                                F.add("R6", "R6", si, sbb, f"حجم الخط {r.font.size.pt:.0f}pt أصغر من الحد الأدنى ({brand.min_size}pt): «{r.text[:30]}».",
                                      f"Text size {r.font.size.pt:.0f}pt is below the minimum ({brand.min_size}pt): \"{r.text[:30]}\".", severity="warn",
                                      method="code: run font size", snippet=r.text[:30], loc=loc)
                            try:
                                rgb = r.font.color.rgb if r.font.color and r.font.color.type is not None else None
                            except Exception:
                                rgb = None
                            if rgb is not None and brand.colors and not is_neutral(tuple(rgb)):
                                if min(rgb_dist(tuple(rgb), hex_to_rgb(c)) for c in brand.colors) > 40:
                                    F.add("R6", "R6", si, sbb, f"لون النص #{rgb} ليس من ألوان الهوية.", f"Text color #{rgb} is not in the brand palette.",
                                          severity="warn", method="code: run color", snippet=str(rgb), loc=loc)
                    ln = 0
                    for w in re.findall(r"\S+", nfkc(text)):
                        tokens.append(Token(w, si, sbb, ar_n > 0, (loc, pi), loc))
                    text_only_typography(F, text, si, loc)
    run_text_rules(F, tokens, brand.numerals, text_arrows=True)
    if brand.fonts:
        allowed = [font_family(f) for f in brand.fonts]
        for fam, uses in font_use.items():
            F.checked("R6", len(uses))
            if not any(a and (a in fam or fam in a) for a in allowed):
                F.add("R6", "R6", uses[0][0], None, f"الخط «{uses[0][2]}» ليس ضمن خطوط الهوية ({'، '.join(brand.fonts)}). استُخدم {len(uses)} مرة.",
                      f"Font \"{uses[0][2]}\" is not a brand font ({', '.join(brand.fonts)}). Used {len(uses)} times.", severity="error",
                      method="code: run font names", snippet=uses[0][2], loc=uses[0][1])
    F.note("R1", "PowerPoint text is plain Unicode that PowerPoint shapes itself, so joining cannot be wrong in the file.")
    F.note("R7", "Page-edge pixel checks need a rendered page. PPTX files are checked on their text and settings, not on rendered pixels.")
    F.note("R8", "Consistency checks need real page geometry, which a PPTX file does not give without rendering it. Export the deck to PDF and upload that.")
    return len(prs.slides)


# ----------------------------------------------------------------- pasted text / images
def analyze_text(text, F, brand):
    tokens = []
    for li, line in enumerate(text.splitlines()):
        ar = has_ar(nfkc(line))
        for w in re.findall(r"\S+", nfkc(line)):
            tokens.append(Token(w, 1, None, ar, ("p", li), f"line {li + 1}"))
        if line.strip():
            text_only_typography(F, line, 1, f"line {li + 1}")
    run_text_rules(F, tokens, brand.numerals, paste_mode=True, text_arrows=True)
    F.note("R2", "Pasted text cannot show how it is laid out, so bidi order cannot be tested here. Upload the PDF to test it.")


def analyze_image_pixels(img, F, brand):
    F.checked("R7")
    for edge, t in edge_strip_check(img):
        W, H = img.size
        box = {"bottom": [0, H - t, W, H], "top": [0, 0, W, t], "left": [0, 0, t, H], "right": [W - t, 0, W, H]}[edge]
        F.add("R7", "R7", 1, box, f"الخلفية لا تغطي الحافة ({ {'bottom':'السفلية','top':'العلوية','left':'اليسرى','right':'اليمنى'}[edge] }): يتبقى شريط أبيض بسمك {t}px.",
              f"The background does not reach the {edge} edge: a white strip {t}px thick remains.", severity="warn", confidence="medium",
              method="code: pixel scan of the edges", snippet=edge)
    if brand.colors:
        small = img.convert("RGB").resize((120, int(120 * img.height / img.width) or 1))
        cols = Counter()
        px = small.load()
        for y in range(small.height):
            for x in range(small.width):
                c = px[x, y]
                if not is_neutral(c):
                    cols[(c[0] // 24, c[1] // 24, c[2] // 24)] += 1
        total = small.width * small.height
        F.checked("R6", len(cols))
        for k, v in cols.most_common(3):
            rgb = tuple(i * 24 + 12 for i in k)
            if v / total > 0.06 and min(rgb_dist(rgb, hex_to_rgb(c)) for c in brand.colors) > 60:
                F.add("R6", "R6", 1, [0, 0, img.width, img.height], f"لون سائد في الصورة ({v * 100 // total}% من المساحة) قريب من #%02x%02x%02x ولا يقترب من أي لون في الهوية." % rgb,
                      f"A dominant color in the image ({v * 100 // total}% of the area), close to #%02x%02x%02x, is not near any brand color." % rgb,
                      severity="warn", confidence="medium", method="code: pixel color histogram", snippet="#%02x%02x%02x" % rgb)
    F.note("R1", "Flat images carry no text layer, so shaping, bidi, numerals and spelling cannot be read by code alone. Paste the text into the text box to check it. Reading text out of images needs a vision model API key (planned for a later version).")
    F.note("R2", "Needs a text layer. Not run on flat images.")
    F.note("R3", "Needs a text layer. Not run on flat images.")
    F.note("R4", "Needs a text layer. Not run on flat images.")
    F.note("R5", "Needs a text layer. Paste the text to check spelling.")
    F.note("R9", "Needs a text layer. Paste the text to check tone.")
    F.note("R8", "Needs a text layer with font sizes. Not run on flat images.")
