"""Tone (register) detection: formal vs friendly/colloquial Arabic. Word-list based (code, no model).
It decides the dominant register of the document, then flags the words that break it and proposes a replacement."""
import re
from collections import Counter

TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")

# casual / colloquial word -> formal replacement (None = delete)
FRIENDLY = {
    "دلوقتي": "الآن", "دلوقت": "الآن", "ازاي": "كيف", "إزاي": "كيف", "ليه": "لماذا", "عايز": "يرغب", "عايزين": "يرغبون",
    "عاوز": "يرغب", "عايزة": "ترغب", "مش": "ليس", "بتاع": "الخاص بـ", "بتاعك": "الخاص بك", "بتاعتك": "الخاصة بك", "بتاعنا": "الخاص بنا",
    "كده": "هكذا", "كدا": "هكذا", "ده": "هذا", "دي": "هذه", "دول": "هؤلاء", "اللي": "الذي", "علشان": "لكي", "عشان": "لكي",
    "يلا": "هيا", "حبيبي": None, "حبيبتي": None, "صاحبي": None, "هلا": "مرحبًا", "اهلا": "أهلًا", "أهلا": "أهلًا",
    "تمام": "حسنًا", "كويس": "جيد", "كويسة": "جيدة", "حلو": "جميل", "حلوة": "جميلة", "جامد": "ممتاز", "برضو": "أيضًا", "برضه": "أيضًا",
    "لسه": "ما زال", "لسا": "ما زال", "بقى": None, "بقا": None, "طب": None, "أوي": "جدًا", "اوي": "جدًا", "خالص": "تمامًا",
    "شوية": "قليلًا", "شوي": "قليلًا", "ايه": "ماذا", "إيه": "ماذا", "فين": "أين", "امتى": "متى", "إمتى": "متى", "مين": "من",
    "هنعمل": "سنعمل", "هنرجعلك": "سنعيد إليك", "هتشوف": "سترى", "هتلاقي": "ستجد", "هتقدر": "ستتمكن", "بنحذف": "نحذف", "بيتمسح": "يُحذف",
    "بيطلع": "يظهر", "بيطلب": "يطلب", "بيشاور": "يشير", "اتفضل": "تفضّل", "اتفضلي": "تفضّلي", "ياريت": "نأمل", "معلش": "عذرًا",
    "شكرا جدا": "شكرًا جزيلًا", "وياك": "معك", "هنا": None,
}
FRIENDLY.pop("هنا", None)
# formal markers
FORMAL = {
    "يرجى": "من فضلك", "يُرجى": "من فضلك", "نتشرف": "يسعدنا", "نتشرّف": "يسعدنا", "نفيدكم": "نخبرك", "نحيطكم": "نخبرك", "تفضلوا": "تفضّل",
    "بموجب": "حسب", "بشأن": "عن", "بخصوص": "عن", "وفقا": "حسب", "وفقًا": "حسب", "السادة": None, "المحترم": None, "المحترمة": None,
    "فضلا": "من فضلك", "فضلاً": "من فضلك", "نود": "نحب", "نأمل": "نتمنى", "نتطلع": "نتمنى", "شاكرين": "شكرًا", "نشكركم": "شكرًا لك",
    "يتوجب": "لازم", "المذكور": None, "سيتم": "هنقوم بـ", "علما": None, "علمًا": None, "نحيطكم": "نخبرك", "الموقرة": None, "الموقر": None,
    "ويسعدنا": "ونفرح", "تحياتنا": "تحياتي", "المتقدم": None,
}
FORMAL_BASE = set(FORMAL) | {"لذا", "إذ", "حيث", "كما", "ولذلك", "لذلك", "إلى", "يتم", "يمكنكم", "نسعد", "الجهة", "المستفيد", "المستفيدين", "يجب", "تتيح", "تتولى"}
FORMAL_BASE -= {"إلى", "حيث", "كما"}  # too common to count


TONE_NOTE = "Tone is judged from word lists of formal and colloquial Arabic (code, not a language model). It sees only the words listed, so it can miss a casual phrase and cannot judge the writing quality. It names the dominant register and flags words that break it."


def _n(w):
    w = TASHKEEL.sub("", w)
    return w.strip("،؛؟!.:()[]{}«»\"'“”-–—/\\|…").replace("ـ", "")


def classify(tokens):
    """tokens: objects with .text; returns list of (token, kind, word) where kind in friendly/formal."""
    out = []
    for t in tokens:
        w = _n(t.text)
        if not w:
            continue
        if EMOJI.search(t.text):
            out.append((t, "friendly", "emoji"))
        elif w in FRIENDLY:
            out.append((t, "friendly", w))
        elif w in FORMAL_BASE:
            out.append((t, "formal", w))
        elif w[:1] in "وف" and w[1:] in FRIENDLY:
            out.append((t, "friendly", w[1:]))
        elif w[:1] in "وف" and w[1:] in FORMAL_BASE:
            out.append((t, "formal", w[1:]))
    return out


def check(F, tokens, cap=12):
    """Adds R9 findings. Returns summary dict (also stored in F.tone)."""
    ar_tokens = [t for t in tokens if re.search(r"[\u0621-\u064A]", t.text) or EMOJI.search(t.text)]
    if len(ar_tokens) < 8:
        return None
    F.checked("R9", len(ar_tokens))
    F.note("R9", TONE_NOTE)
    hits = classify(ar_tokens)
    nf = sum(1 for _, k, _ in hits if k == "friendly")
    nm = sum(1 for _, k, _ in hits if k == "formal")
    tot = nf + nm
    summ = dict(friendly=nf, formal=nm, dominant="unclear", words=len(ar_tokens))
    if tot < 3:
        F.tone = summ
        return summ
    share = nm / tot
    if share >= 0.65 and nm >= 2:
        summ["dominant"] = "formal"
    elif (1 - share) >= 0.65 and nf >= 2:
        summ["dominant"] = "friendly"
    else:
        summ["dominant"] = "mixed"
    F.tone = summ
    dom = summ["dominant"]
    if dom == "mixed":
        F.add("R9", "R9", 1, None,
              f"النبرة غير موحدة: يضم النص {nm} من العبارات الرسمية و{nf} من العبارات الودّية. يُفضَّل اختيار نبرة واحدة للنص كله.",
              f"The tone is mixed: {nm} formal and {nf} friendly markers in the same text. Pick one register for the whole text.",
              severity="warn", confidence="low", method="code: word lists for formal and colloquial Arabic (not a language model)",
              snippet=f"formal {nm} / friendly {nf}")
        F.items[-1]["fix_ar"] = "اختر نبرة واحدة (رسمية أو ودّية) وعدّل العبارات المخالفة لها في النص."
        F.items[-1]["fix_en"] = "Choose one tone (formal or friendly) and rewrite the phrases that break it."
        return summ
    minority = "friendly" if dom == "formal" else "formal"
    seen = 0
    for t, k, w in hits:
        if k != minority or seen >= cap:
            continue
        seen += 1
        if dom == "formal":
            rep = "احذف الرمز التعبيري." if w == "emoji" else None
            r = FRIENDLY.get(w)
            msg = f"عبارة ودّية «{_n(t.text) if w != 'emoji' else t.text}» داخل نص نبرته السائدة رسمية ({nm} من {tot} عبارة ذات دلالة)."
            en = f"Friendly word \"{t.text}\" in a text whose dominant tone is formal ({nm} of {tot} register markers)."
            if w == "emoji":
                fx_ar, fx_en = "احذف الرمز التعبيري ليتسق مع النبرة الرسمية.", "Remove the emoji to match the formal tone."
            elif r:
                fx_ar, fx_en = f"استبدلها بـ «{r}» لتوافق النبرة الرسمية.", f"Replace with \"{r}\" to match the formal tone."
            else:
                fx_ar, fx_en = "احذفها أو أعد صياغة الجملة بعبارة رسمية.", "Delete it or rephrase the sentence formally."
            conf = "medium"
        else:
            r = FORMAL.get(w)
            msg = f"عبارة رسمية «{_n(t.text)}» داخل نص نبرته السائدة ودّية ({nf} من {tot} عبارة ذات دلالة)."
            en = f"Formal word \"{t.text}\" in a text whose dominant tone is friendly ({nf} of {tot} register markers)."
            if r:
                fx_ar, fx_en = f"استبدلها بـ «{r}» لتوافق النبرة الودّية.", f"Replace with \"{r}\" to match the friendly tone."
            else:
                fx_ar, fx_en = "احذفها أو أعد صياغة الجملة بنبرة ودّية.", "Delete it or rephrase the sentence in a friendlier tone."
            conf = "low"
        F.add("R9", "R9", t.page, t.bbox, msg, en, severity="warn", confidence=conf,
              method="code: word lists for formal and colloquial Arabic (not a language model)", snippet=_n(t.text), loc=t.loc)
        F.items[-1]["fix_ar"], F.items[-1]["fix_en"] = fx_ar, fx_en
    return summ
