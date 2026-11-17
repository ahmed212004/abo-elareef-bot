import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

CAIRO_TZ = ZoneInfo("Africa/Cairo")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
}

EGYPT_COORDS_MAP = {
    "الفيوم": (29.3084, 30.8428),
    "فيوم": (29.3084, 30.8428),
    "faiyum": (29.3084, 30.8428),
    "fayoum": (29.3084, 30.8428),
    "القاهرة": (30.0444, 31.2357),
    "قاهرة": (30.0444, 31.2357),
    "cairo": (30.0444, 31.2357),
    "الجيزة": (30.0131, 31.2089),
    "جيزة": (30.0131, 31.2089),
    "giza": (30.0131, 31.2089),
    "الإسكندرية": (31.2001, 29.9187),
    "الاسكندرية": (31.2001, 29.9187),
    "اسكندرية": (31.2001, 29.9187),
    "alexandria": (31.2001, 29.9187),
    "المنصورة": (31.0409, 31.3785),
    "mansoura": (31.0409, 31.3785),
    "طنطا": (30.7865, 31.0004),
    "tanta": (30.7865, 31.0004),
    "الزقازيق": (30.5877, 31.5020),
    "zagazig": (30.5877, 31.5020),
    "أسوان": (24.0889, 32.8998),
    "اسوان": (24.0889, 32.8998),
    "aswan": (24.0889, 32.8998),
    "أسيوط": (27.1809, 31.1837),
    "اسيوط": (27.1809, 31.1837),
    "asyut": (27.1809, 31.1837),
    "المنيا": (28.1099, 30.7503),
    "minya": (28.1099, 30.7503),
    "بني سويف": (29.0661, 31.0994),
    "بنى سويف": (29.0661, 31.0994),
    "سوهاج": (26.5569, 31.6948),
    "قنا": (26.1551, 32.7160),
    "الأقصر": (25.6872, 32.6396),
    "الاقصر": (25.6872, 32.6396),
    "بورسعيد": (31.2653, 32.3019),
    "الإسماعيلية": (30.5965, 32.2715),
    "الاسماعيلية": (30.5965, 32.2715),
    "السويس": (29.9668, 32.5498),
    "دمياط": (31.4175, 31.8144),
    "كفر الشيخ": (31.1107, 30.9388),
    "دمنهور": (31.0341, 30.4682),
    "بنها": (30.4660, 31.1848),
    "شبين الكوم": (30.5503, 31.0106),
    "الغردقة": (27.2579, 33.8116),
    "شرم الشيخ": (27.9158, 34.3300),
    "العريش": (31.1321, 33.8033),
    "مرسى مطروح": (31.3543, 27.2373),
}


def resolve_egypt_city(city_input):
    clean = (city_input or "Faiyum").strip()
    if clean.lower() in EGYPT_COORDS_MAP:
        return clean.lower()
    return clean


def format_12h_ar(hhmm):
    try:
        h, m = map(int, hhmm.split(":"))
        suffix = "ص" if h < 12 else "م"
        h12 = h % 12 or 12
        return f"{h12:02d}:{m:02d} {suffix}"
    except Exception:
        return hhmm


def get_prayer_times(city="Faiyum"):
    """جلب مواقيت الصلاة بالإحداثيات الدقيقة بتوقيت الهيئة المصرية العامة للمساحة"""
    key = (city or "faiyum").strip().lower()
    coords = EGYPT_COORDS_MAP.get(key)

    try:
        if coords:
            lat, lon = coords
            url = "https://api.aladhan.com/v1/timings"
            params = {"latitude": lat, "longitude": lon, "method": 5}
        else:
            url = "https://api.aladhan.com/v1/timingsByCity"
            params = {"city": city, "country": "Egypt", "method": 5}

        res = requests.get(url, params=params, timeout=12).json()
        if res.get("code") != 200:
            return None

        timings = res["data"]["timings"]
        keys = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
        clean_timings = {}
        for k in keys:
            raw = timings.get(k, "").split()[0]
            if raw:
                clean_timings[k] = raw
        return clean_timings if "Fajr" in clean_timings else None
    except Exception:
        return None


def get_next_prayer_info(timings_24h):
    if not timings_24h:
        return None
    now = datetime.now(CAIRO_TZ)
    prayer_names = [
        ("Fajr", "الفجر"),
        ("Dhuhr", "الظهر"),
        ("Asr", "العصر"),
        ("Maghrib", "المغرب"),
        ("Isha", "العشاء"),
    ]
    for key, ar_name in prayer_names:
        t_str = timings_24h.get(key)
        if not t_str:
            continue
        h, m = map(int, t_str.split(":"))
        p_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if p_dt > now:
            diff_mins = int((p_dt - now).total_seconds() // 60)
            hrs, mins = divmod(diff_mins, 60)
            rem_txt = f"{hrs} ساعة و {mins} دقيقة" if hrs > 0 else f"{mins} دقيقة"
            return {"name": ar_name, "time_12": format_12h_ar(t_str), "remaining": rem_txt}

    return {"name": "الفجر (غداً)", "time_12": format_12h_ar(timings_24h.get("Fajr", "")), "remaining": "بعد منتصف الليل"}


def get_weather_info():
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=29.3084&longitude=30.8428"
            "&current=temperature_2m,relative_humidity_2m,weather_code"
            "&timezone=Africa%2FCairo"
        )
        res = requests.get(url, timeout=10).json()
        curr = res.get("current", {})
        temp = curr.get("temperature_2m")
        hum = curr.get("relative_humidity_2m")
        code = curr.get("weather_code", 0)

        code_map = {
            0: "صافي ومشمس ☀️",
            1: "صافي غالباً 🌤️",
            2: "غائم جزئياً ⛅",
            3: "غائم ☁️",
            45: "شبورة خفيفة 🌫️",
            48: "ضباب 🌫️",
            51: "رذاذ خفيف 🌦️",
            61: "أمطار خفيفة 🌧️",
            63: "أمطار متوسطة 🌧️",
            80: "زخات مطر 🌦️",
        }
        desc = code_map.get(code, "معتدل 🌤️")
        if temp is not None:
            return f"{temp}°C ({desc} | رطوبة {hum}%)"
    except Exception:
        pass
    return None


def get_usd_to_egp():
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10).json()
        rate = res.get("rates", {}).get("EGP")
        return round(float(rate), 2) if rate else None
    except Exception:
        return None


def get_egypt_gold_prices():
    urls = [
        "https://egypt.gold-price-today.com/",
        "https://sa3r-elzahab.com/",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(" ", strip=True)

            prices = {}
            for karat in ("24", "21", "18"):
                m = re.search(rf"عيار\s*{karat}\D{{0,25}}?(\d{{3,5}}(?:[.,]\d+)?)", text)
                if m:
                    prices[f"k{karat}"] = float(m.group(1).replace(",", ""))

            m_pound = re.search(r"الجنيه\s*الذهب\D{0,25}?(\d{4,6}(?:[.,]\d+)?)", text)
            if m_pound:
                prices["pound"] = float(m_pound.group(1).replace(",", ""))

            if "k21" in prices:
                return prices
        except Exception:
            continue
    return None


def check_product_price(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        title = soup.title.string.strip()[:70] if soup.title and soup.title.string else "منتج مراقب"

        meta_price = soup.find("meta", {"property": "product:price:amount"}) or soup.find("meta", {"itemprop": "price"})
        if meta_price and meta_price.get("content"):
            val = float(re.sub(r"[^\d.]", "", meta_price["content"]))
            if val > 0:
                return {"title": title, "price": val}

        selectors = [
            "span.a-price-whole",
            "div[data-qa='div-price-now']",
            "span.priceNow",
            "span.price",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                raw_num = re.sub(r"[^\d.]", "", el.get_text())
                if raw_num:
                    val = float(raw_num)
                    if val > 0:
                        return {"title": title, "price": val}

        text = soup.get_text(" ", strip=True)
        m = re.search(r"(?:EGP|جنيه|ج\.م)\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)|(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:EGP|جنيه|ج\.م)", text)
        if m:
            num_str = (m.group(1) or m.group(2)).replace(",", "")
            return {"title": title, "price": float(num_str)}

        return {"title": title, "price": None}
    except Exception:
        return None


def scan_jobs_page(page_url, keywords_csv):
    keywords = [k.strip().lower() for k in keywords_csv.split(",") if k.strip()]
    if not keywords:
        return []

    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        matches = []

        for a in soup.find_all("a", href=True):
            link_text = a.get_text(" ", strip=True)
            href = urljoin(page_url, a["href"])
            combined = f"{link_text} {href}".lower()

            if len(link_text) >= 6 and any(kw in combined for kw in keywords):
                matches.append((link_text[:80], href))

        unique = {}
        for title, link in matches:
            unique[link] = title
        return list(unique.items())
    except Exception:
        return []


def fetch_article_text(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "h1", "h2", "li"])]
        clean_text = "\n".join(p for p in paragraphs if len(p) > 25)
        return clean_text[:9000] if clean_text else None
    except Exception:
        return None