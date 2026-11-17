import base64
import os
import requests
from database import get_setting
from trackers import fetch_article_text

DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "PUT_YOUR_GEMINI_KEY_HERE").strip()
BASE_API = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.0-flash",
]


def _get_api_key(ws_id=None):
    if ws_id:
        ws_key = get_setting(ws_id, "gemini_api_key", "").strip()
        if ws_key:
            return ws_key

    global_key = get_setting("global", "gemini_api_key", "").strip()
    if global_key:
        return global_key

    if DEFAULT_GEMINI_KEY and DEFAULT_GEMINI_KEY != "PUT_YOUR_GEMINI_KEY_HERE":
        return DEFAULT_GEMINI_KEY
    return ""


def _discover_flash_model(api_key):
    """اكتشاف أحدث موديل Flash متاح تلقائياً في حال تغيير الإصدارات مستقبلاً"""
    try:
        res = requests.get(f"{BASE_API}/models?key={api_key}", timeout=15).json()
        for m in res.get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "flash" in name.lower() and "generateContent" in methods:
                return name
    except Exception:
        pass
    return None


def _call_gemini(parts, system_prompt=None, ws_id=None):
    api_key = _get_api_key(ws_id)
    if not api_key:
        return (
            "⚠️ ميزة الذكاء الاصطناعي والصوت محتاجة مفتاح Gemini API مجاني.\n\n"
            "1. هات مفتاح مجاني من: aistudio.google.com\n"
            "2. ابعتهولي هنا بالشكل ده:\n"
            "/setkey AIzaSyYourKeyHere"
        )

    payload = {"contents": [{"parts": parts}]}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    last_error = ""

    for model_name in PREFERRED_MODELS:
        url = f"{BASE_API}/models/{model_name}:generateContent?key={api_key}"
        try:
            res = requests.post(url, json=payload, timeout=30).json()
            if "error" in res:
                err_msg = res["error"].get("message", "")
                last_error = err_msg
                if "no longer available" in err_msg or "not found" in err_msg.lower():
                    continue
                return f"❌ خطأ من Gemini: {err_msg}"

            candidates = res.get("candidates", [])
            if candidates:
                return candidates[0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            return f"⚠️ تعذر الاتصال بـ Gemini حالياً: {e}"

    # محاولة أخيرة باكتشاف الموديل المتاح تلقائياً من حسابك
    auto_model = _discover_flash_model(api_key)
    if auto_model and auto_model not in PREFERRED_MODELS:
        try:
            url = f"{BASE_API}/models/{auto_model}:generateContent?key={api_key}"
            res = requests.post(url, json=payload, timeout=30).json()
            candidates = res.get("candidates", [])
            if candidates:
                return candidates[0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass

    return f"❌ خطأ من Gemini: {last_error or 'تعذر الوصول للموديل حالياً.'}"


def ask_gemini_chat(user_text, ws_id=None):
    sys_prompt = (
        "أنت 'أبو العريف'، مساعد شخصي ذكي وخفيف الظل. "
        "رد باللهجة المصرية الطبيعية بشكل عملي ومباشر ومختصر (بدون مقدمات طويلة أو حشو)."
    )
    return _call_gemini([{"text": user_text}], system_prompt=sys_prompt, ws_id=ws_id)


def summarize_article_url(url, ws_id=None):
    article_text = fetch_article_text(url)
    if not article_text:
        return "❌ مقدرتش أقرأ محتوى الرابط ده (ممكن يكون محمي أو بيحتاج تسجيل دخول)."

    prompt = (
        "لخص المقال التالي في 5 نقط واضحة ومختصرة باللهجة المصرية، "
        "وركز على أهم المعلومات العملية والخلاصة:\n\n"
        f"{article_text}"
    )
    return "📝 ملخص الرابط:\n\n" + _call_gemini([{"text": prompt}], ws_id=ws_id)


def transcribe_voice_note(ogg_bytes, ws_id=None):
    b64_audio = base64.b64encode(ogg_bytes).decode("utf-8")
    parts = [
        {"inline_data": {"mime_type": "audio/ogg", "data": b64_audio}},
        {
            "text": (
                "اسمع الرسالة الصوتية دي واكتب الكلام المسموع بالظبط باللهجة المصرية "
                "وبالأرقام الحسابية (مثلاً 85 مش خمسة وتمانين) من غير أي مقدمات أو تعليقات. "
                "لو الصوت فاضي أو مش مفهوم اكتب كلمة واحدة: UNCLEAR"
            )
        },
    ]
    res = _call_gemini(parts, ws_id=ws_id)
    if res.startswith(("⚠️", "❌")) or "UNCLEAR" in res:
        return None, res
    return res.strip(), None