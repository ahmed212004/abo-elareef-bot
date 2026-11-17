import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize_digits(text):
    return text.translate(ARABIC_DIGITS)


def extract_standalone_url(text):
    """التحقق مما إذا كانت الرسالة عبارة عن رابط مقال للتلخيص"""
    clean = text.strip()
    if "#" in clean or " " in clean:
        return None
    if re.match(r"^https?://\S+$", clean):
        return clean
    return None


def detect_expense_category(item_text):
    t = item_text.lower()
    categories = {
        "أكل ومشروبات": ["قهوة", "شاي", "غدا", "فطار", "عشا", "أكل", "اكل", "مطعم", "سوبرماركت", "بيتزا", "كشري", "سندوتش", "عصير", "مياه", "مية", "سناكس"],
        "مواصلات وبنزين": ["أوبر", "اوبر", "ديدي", "مواصلات", "مترو", "ميكروباص", "تاكسي", "بنزين", "ركنة", "سايس", "سفر"],
        "فواتير واشتراكات": ["نت", "باقة", "كهرباء", "غاز", "شحن", "رصيد", "اشتراك", "فاتورة"],
        "سلف والتزامات": ["سلفة", "جمعية", "قسط", "دين"],
    }
    for cat, words in categories.items():
        if any(w in t for w in words):
            return cat
    return "أخرى"


def parse_expense(text):
    """تحليل رسائل مثل: 'غدا 120' أو '50 قهوة' أو 'صرفت 85 جنيه بنزين'"""
    clean = normalize_digits(text).strip()
    if "http://" in clean or "https://" in clean or "#" in clean:
        return None
    if clean.startswith(("فكرني", "ذكرني", "/")):
        return None

    # إزالة كلمات زائدة شائعة في الكلام الصوتي
    clean = re.sub(r"^(?:صرفت|دفعت|حاسبت|اشتريت)\s+", "", clean)
    clean = re.sub(r"\b(?:جنيه|جنيهاً|ج\.م)\b", "", clean).strip()

    match = re.search(r"(\d+(?:\.\d+)?)", clean)
    if not match:
        return None

    amount = float(match.group(1))
    if amount <= 0:
        return None

    item = (clean[:match.start()] + " " + clean[match.end():]).strip()
    item = re.sub(r"\s+", " ", item).strip("-:، ")
    if not item or len(item) > 60:
        return None

    category = detect_expense_category(item)
    return {"item": item, "amount": amount, "category": category}


def parse_reminder(text):
    """تحليل التذكيرات لمرة واحدة (بعد ...) أو المتكررة (كل ...)"""
    clean = normalize_digits(text).strip()
    if not re.match(r"^(?:فكرني|ذكرني)\b", clean):
        return None

    body = re.sub(r"^(?:فكرني|ذكرني)\s*(?:بـ|ب)?\s*", "", clean).strip()
    now = datetime.now(CAIRO_TZ)

    rel_pattern = (
        r"\b(بعد|كل)\s+(?:"
        r"(?:(نص|نصف)\s+دقيق[ةه])|"
        r"(\d+)\s*(ثاني[ةه]|ثواني|ثوانى|ثوان)|"
        r"(?:(ربع)\s+ساع[ةه])|"
        r"(?:(تلت|ثلث)\s+ساع[ةه])|"
        r"(?:(نص|نصف)\s+ساع[ةه])|"
        r"(?:ساع[ةه]\s*و\s*(نص|نصف))|"
        r"(دقيقتين)|"
        r"(ساعتين)|"
        r"(يومين)|"
        r"(\d+)\s*(دقيق[ةه]|دقايق|دقائق|ساع[ةه]|ساعات|يوم|أيام|ايام)|"
        r"(دقيق[ةه]|ساع[ةه]|يوم)"
        r")"
    )
    rel_match = re.search(rel_pattern, body)
    if rel_match:
        mode = rel_match.group(1)
        matched_phrase = rel_match.group(0)
        task = (body[:rel_match.start()] + " " + body[rel_match.end():]).strip(" ،-:")
        task = re.sub(r"\s+", " ", task) or "تذكير مهم"

        if rel_match.group(2):
            delta = timedelta(seconds=30)
        elif rel_match.group(3):
            delta = timedelta(seconds=int(rel_match.group(3)))
        elif rel_match.group(5):
            delta = timedelta(minutes=15)
        elif rel_match.group(6):
            delta = timedelta(minutes=20)
        elif rel_match.group(7):
            delta = timedelta(minutes=30)
        elif rel_match.group(8):
            delta = timedelta(minutes=90)
        elif rel_match.group(9):
            delta = timedelta(minutes=2)
        elif rel_match.group(10):
            delta = timedelta(hours=2)
        elif rel_match.group(11):
            delta = timedelta(days=2)
        elif rel_match.group(12):
            num = int(rel_match.group(12))
            unit = rel_match.group(13)
            if "دقيق" in unit or "دقائ" in unit or "دقاي" in unit:
                delta = timedelta(minutes=num)
            elif "ساع" in unit:
                delta = timedelta(hours=num)
            else:
                delta = timedelta(days=num)
        else:
            single = rel_match.group(14)
            if single.startswith("دقيق"):
                delta = timedelta(minutes=1)
            elif single.startswith("ساع"):
                delta = timedelta(hours=1)
            else:
                delta = timedelta(days=1)

        repeat_sec = int(delta.total_seconds()) if mode == "كل" else 0
        return {
            "text": task,
            "remind_dt": now + delta,
            "repeat_seconds": repeat_sec,
            "repeat_label": matched_phrase if mode == "كل" else None,
        }

    hour = 9
    minute = 0
    spans = []
    time_match = re.search(r"الساعة\s+(\d{1,2})(?::(\d{2}))?\s*(الصبح|صباحا|الضهر|العصر|المغرب|بالليل|مساء)?", body)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        period = time_match.group(3) or ""
        if period in ("العصر", "المغرب", "بالليل", "مساء") and hour < 12:
            hour += 12
        elif period == "الضهر" and hour < 11:
            hour += 12
        elif period in ("الصبح", "صباحا") and hour == 12:
            hour = 0
        spans.append(time_match.span())

    day_match = re.search(r"يوم\s+(\d{1,2})", body)
    tomorrow_match = re.search(r"\b(?:بكرة|غدا)\b", body)

    target_date = now.date()
    if day_match:
        spans.append(day_match.span())
        day_num = min(max(int(day_match.group(1)), 1), 28)
        if day_num < now.day:
            next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=day_num)
            target_date = next_month.date()
        else:
            target_date = now.date().replace(day=day_num)
    elif tomorrow_match:
        spans.append(tomorrow_match.span())
        target_date = (now + timedelta(days=1)).date()
    elif time_match:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            target_date = (now + timedelta(days=1)).date()
    else:
        return None

    task = body
    for start, end in sorted(spans, reverse=True):
        task = task[:start] + " " + task[end:]
    task = re.sub(r"\s+", " ", task).strip(" ،-:") or "تذكير مهم"

    target_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=CAIRO_TZ)
    return {"text": task, "remind_dt": target_dt, "repeat_seconds": 0, "repeat_label": None}


def parse_vault_message(text):
    """استخراج الهاشتاج والمحتوى للذاكرة التانية"""
    tags = re.findall(r"#([\w\u0600-\u06FF]+)", text)
    if not tags:
        return None
    main_tag = tags[0]
    clean_content = re.sub(r"#[\w\u0600-\u06FF]+", "", text).strip()
    if not clean_content:
        return None
    return {"tag": main_tag, "content": clean_content}