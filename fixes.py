"""Correction suggestions: for each finding, what the right version is. Rule-based (code), derived from the finding itself."""
import re

IND = "٠١٢٣٤٥٦٧٨٩"
WES = "0123456789"
PER = "۰۱۲۳۴۵۶۷۸۹"
LEFT_ARROW = {"→": "←", "⇒": "⇐", "➜": "←", "➔": "←", "➞": "←", "➡": "⬅", "▶": "◀", "►": "◄", "»": "«", "›": "‹"}


def _conv(s, target):
    out = []
    for c in s:
        if c in IND: d = IND.index(c)
        elif c in WES: d = WES.index(c)
        elif c in PER: d = PER.index(c)
        else:
            out.append(c); continue
        out.append({"indic": IND, "west": WES}[target][d])
    return "".join(out)


def _hex_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _nearest(hx, colors):
    try:
        r = _hex_rgb(hx)
        best = min(colors, key=lambda c: sum((a - b) ** 2 for a, b in zip(r, _hex_rgb(c))))
        return best
    except Exception:
        return None


def suggest(it, brand=None):
    """returns (fix_ar, fix_en) or (None, None)"""
    m_ar = it.get("message_ar", "")
    key = it.get("check")
    snip = it.get("snippet", "") or ""
    rule = it.get("rule")
    m = None
    if rule == "R8":
        if (m := re.search(r"([\d.]+)pt\s*vs\s*([\d.]+)pt", snip)):
            a, b = m.group(1), m.group(2)
            return (f"إن لم يكن الفرق مقصودًا، اجعل حجم هذه الفقرة {b}pt مثل الفقرة المجاورة.",
                    f"If the difference is not intended, set this paragraph to {b}pt like its neighbour.")
        if snip == "weight":
            bold = "وزنه مختلف: عريض" in m_ar
            return (("اجعل العنصر بالوزن العادي مثل بقية العناصر." if bold else "اجعل العنصر بالوزن العريض مثل بقية العناصر."),
                    ("Set it to regular weight like the others." if bold else "Set it to bold like the others."))
        if (m := re.search(r"(right|left) edge ([\d]+)pt", snip)):
            return (f"حاذِ العنصر مع الحافة المشتركة لبقية العناصر (يلزم تحريكه نحو {m.group(2)}pt).",
                    f"Align it with the shared edge of the other items (move it about {m.group(2)}pt).")
        if (m := re.search(r"نحو (\d+)pt", m_ar)):
            return (f"اجعل المسافة نحو {m.group(1)}pt مثل بقية المسافات في المجموعة.",
                    f"Set the gap to about {m.group(1)}pt like the other gaps in the group.")
    if rule == "R9":
        return None, None
    if key == "R5":
        if (m := re.search(r"والصواب «([^»]+)»", m_ar)) or (m := re.search(r"الصواب «([^»]+)»", m_ar)):
            return (f"اكتبها «{m.group(1)}».", f"Write \"{m.group(1)}\".")
        if "علامة ترقيم لاتينية" in m_ar:
            fixed = snip.translate(str.maketrans({",": "،", ";": "؛", "?": "؟"}))
            return (f"اكتبها «{fixed}».", f"Write \"{fixed}\".")
        if "كلمة مكررة" in m_ar:
            return (f"احذف التكرار واكتب «{snip}» مرة واحدة.", f"Delete the repeat and write \"{snip}\" once.")
        if "مسافات مزدوجة" in m_ar:
            return ("احذف المسافة الزائدة واترك مسافة واحدة بين الكلمات.", "Remove the extra space; keep a single space between words.")
        if "مسافة قبل علامة" in m_ar:
            return ("احذف المسافة قبل علامة الترقيم، فتُكتب العلامة ملاصقة للكلمة السابقة.", "Remove the space before the punctuation mark so it touches the previous word.")
        if "لا توجد مسافة بعد" in m_ar:
            return ("أضف مسافة بعد علامة الترقيم.", "Add a space after the punctuation mark.")
    if key == "R3":
        if "مكتوب بالأرقام" in m_ar:
            tgt = "indic" if re.search(r"بينما[^.]*الهندية", m_ar) else "west"
            m = re.search(r"الرقم «([^»]+)»", m_ar)
            if m:
                fixed = _conv(m.group(1), tgt)
                nm = "الهندية" if tgt == "indic" else "الغربية"
                return (f"اكتبه «{fixed}» بالأرقام {nm}.", f"Write \"{fixed}\" in {'Arabic-Indic' if tgt == 'indic' else 'Western'} digits.")
        if "علامة النسبة" in m_ar:
            fixed = snip.replace("%", "٪")
            return (f"اكتبها «{fixed}».", f"Write \"{fixed}\".")
        if (m := re.search(r"تظهر العملة «([^»]+)» قبل المبلغ «([^»]+)»", m_ar)):
            return (f"اكتبها «{m.group(2)} {m.group(1)}».", f"Write \"{m.group(2)} {m.group(1)}\".")
        if "داخل صورة" in m_ar:
            m = re.search(r"الرقم «([^»]+)»", m_ar)
            if m:
                return (f"عدّل الرقم داخل الصورة إلى «{_conv(m.group(1), 'indic')}» (بالأرقام الهندية) ثم أعد تصدير الصورة.",
                        f"Change the number inside the image to \"{_conv(m.group(1), 'indic')}\" (Arabic-Indic digits) and re-export it.")
    if key == "R1":
        m = re.search(r"في «([^»]+)»", m_ar)
        w = m.group(1) if m else snip
        return (f"أعد كتابة «{w}» كنص Unicode عاديًا بدل حروف منفصلة، ثم أعد التصدير مع تفعيل مُنسِّق النص العربي (Middle Eastern أو World-Ready).",
                f"Retype \"{w}\" as plain Unicode text instead of isolated letters, then re-export with the Arabic text composer enabled (Middle Eastern / World-Ready).")
    if key == "R2":
        if "الأقواس" in m_ar:
            return ("اجعل اتجاه الفقرة RTL ثم أعد إدخال القوسين ليعكسهما البرنامج تلقائيًا.", "Set the paragraph to RTL and retype the brackets so the app mirrors them.")
        if "لاتينية معكوسة" in m_ar:
            return ("اجعل اتجاه الفقرة RTL وأعد كتابة الكلمة اللاتينية بترتيبها الصحيح من اليسار إلى اليمين.", "Set the paragraph to RTL and retype the Latin word in its correct left-to-right order.")
        if "علامة الترقيم" in m_ar:
            return (f"اجعل اتجاه الفقرة RTL لتنتقل العلامة «{snip}» إلى نهاية الجملة.", f"Set the paragraph to RTL so \"{snip}\" moves to the end of the sentence.")
        if "RTL" in m_ar:
            return ("فعّل اتجاه RTL للفقرة (في PowerPoint: الصفحة الرئيسية ثم زر «اتجاه النص من اليمين إلى اليسار»).",
                    "Turn on RTL for the paragraph (PowerPoint: Home, then the right-to-left text direction button).")
    if key == "R4":
        if "السهم" in m_ar:
            a = LEFT_ARROW.get(snip.strip()[:1], "←")
            return (f"استبدله بسهم «{a}» ليشير إلى الاتجاه الصحيح في التصميم من اليمين إلى اليسار.", f"Replace it with \"{a}\" so it points the right way in an RTL layout.")
        if "محاذاة" in m_ar or "محاذاتها" in m_ar:
            return ("اجعل محاذاة الفقرة إلى اليمين مثل بقية الفقرات العربية.", "Align the paragraph to the right like the other Arabic paragraphs.")
    if key == "R6":
        cols = list(getattr(brand, "colors", None) or [])
        fonts = list(getattr(brand, "fonts", None) or [])
        if "ليس ضمن خطوط الهوية" in m_ar:
            if fonts:
                return (f"استبدل الخط بأحد خطوط الهوية: {'، '.join(fonts)}.", f"Replace it with a brand font: {', '.join(fonts)}.")
        if "أصغر من الحد الأدنى" in m_ar and getattr(brand, "min_size", None):
            return (f"كبّر الخط إلى {brand.min_size}pt على الأقل.", f"Increase the size to at least {brand.min_size}pt.")
        if "ليس من ألوان الهوية" in m_ar or "لا يقترب" in m_ar:
            hx = None
            mm = re.search(r"#[0-9a-fA-F]{6}", m_ar) or re.search(r"#[0-9a-fA-F]{6}", snip)
            if mm: hx = mm.group(0)
            elif snip.startswith("(") or re.fullmatch(r"\(?\d+, ?\d+, ?\d+\)?", snip): hx = None
            n = _nearest(hx, cols) if (hx and cols) else None
            if n:
                return (f"استخدم أقرب لون في الهوية: {n}.", f"Use the closest brand color: {n}.")
            if cols:
                return (f"استخدم أحد ألوان الهوية: {'، '.join(cols)}.", f"Use a brand color: {', '.join(cols)}.")
    if key == "R7":
        return ("مدّ الخلفية حتى حافة الصفحة بالكامل (ويُفضَّل إضافة 3 مم تجاوز Bleed) لإزالة الشريط الأبيض.",
                "Extend the background to the page edge (ideally with 3 mm bleed) to remove the white strip.")
    return None, None
