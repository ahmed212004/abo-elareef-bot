import calendar
import csv
import io
import json
import os
import re
import threading
import time
from datetime import datetime
import requests
from flask import Flask

from database import (
    CAIRO_TZ,
    DB_PATH,
    init_db,
    register_or_get_user,
    create_shared_workspace,
    join_shared_workspace,
    switch_workspace_mode,
    leave_shared_workspace,
    get_workspace_details,
    get_workspace_recipients,
    get_all_active_workspaces,
    get_setting,
    set_setting,
    add_expense,
    undo_last_expense,
    get_expenses_summary,
    get_all_expenses,
    add_bill,
    get_bills,
    delete_bill,
    mark_bill_paid,
    get_due_bills,
    add_reminder,
    get_due_reminders,
    get_upcoming_reminders,
    get_today_reminders,
    delete_reminder,
    add_vault_item,
    get_vault_tags,
    get_vault_items_by_tag,
    search_vault,
    add_watcher,
    get_watchers,
    delete_watcher,
    is_link_seen,
)
from parsers import (
    parse_expense,
    parse_reminder,
    parse_vault_message,
    normalize_digits,
    extract_standalone_url,
)
from trackers import (
    get_prayer_times,
    get_next_prayer_info,
    format_12h_ar,
    resolve_egypt_city,
    get_weather_info,
    get_usd_to_egp,
    get_egypt_gold_prices,
    check_product_price,
    scan_jobs_page,
)
from ai_brain import ask_gemini_chat, summarize_article_url, transcribe_voice_note

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9227316489:AAGuB_sk_gFHCOB8OCVS0yZ3vzvlsghNDgE").strip()

user_states = {}


def get_main_menu(user):
    if user["is_shared_mode"]:
        top_row = [
            {"text": "👤 التبديل لحسابي الشخصي", "callback_data": "mode_personal"},
            {"text": "👨‍👩‍👧‍👦 إعدادات العائلة", "callback_data": "menu_shared"},
        ]
    elif user["shared_ws"]:
        top_row = [
            {"text": "👨‍👩‍👧‍👦 التبديل للمساحة المشتركة", "callback_data": "mode_shared"},
            {"text": "⚙️ إدارة المساحة", "callback_data": "menu_shared"},
        ]
    else:
        top_row = [
            {"text": "👥 إنشاء / انضمام لمساحة مشتركة", "callback_data": "menu_shared"},
        ]

    return {
        "inline_keyboard": [
            top_row,
            [
                {"text": "💰 المصاريف والميزانية", "callback_data": "menu_expenses"},
                {"text": "🧾 الفواتير والأقساط", "callback_data": "menu_bills"},
            ],
            [
                {"text": "⏰ التذكيرات", "callback_data": "menu_reminders"},
                {"text": "🗂️ الذاكرة التانية", "callback_data": "menu_vault"},
            ],
            [
                {"text": "🕌 مواقيت الصلاة", "callback_data": "menu_prayer"},
                {"text": "🪙 الذهب والدولار", "callback_data": "menu_gold"},
            ],
            [
                {"text": "🛒 صائد الخصومات", "callback_data": "menu_products"},
                {"text": "💼 رادار الوظائف", "callback_data": "menu_jobs"},
            ],
            [
                {"text": "💾 تصدير بياناتي", "callback_data": "do_backup"},
            ],
        ]
    }


def mode_badge(user):
    if user["is_shared_mode"]:
        details = get_workspace_details(user["active_ws"])
        name = details["name"] if details else "المساحة المشتركة"
        return f"📍 الوضع الحالي: 👨‍👩‍👧‍👦 {name}\n"
    return "📍 الوضع الحالي: 👤 حسابك الشخصي\n"


def tg_request(method, payload=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if files:
            return requests.post(url, data=payload, files=files, timeout=25).json()
        return requests.post(url, json=payload, timeout=25).json()
    except Exception:
        return {}


def send_msg(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    return tg_request("sendMessage", payload)


def edit_msg(chat_id, message_id, text, markup):
    res = tg_request(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": markup,
            "disable_web_page_preview": True,
        },
    )
    if not res.get("ok"):
        send_msg(chat_id, text, markup)


def get_cached_prayer_times(city="Faiyum"):
    """تخزين مواقيت الصلاة لليوم في قاعدة البيانات لتجنب استهلاك الـ API كل 10 ثواني"""
    today_str = datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    eng_city = resolve_egypt_city(city)
    cache_key = f"prayer_coords_{today_str}_{eng_city.lower()}"

    cached = get_setting("global", cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    timings = get_prayer_times(eng_city)
    if timings:
        set_setting("global", cache_key, json.dumps(timings))
    return timings


def is_prayer_alerts_enabled(ws_id):
    default_val = "0" if ws_id.startswith("shared_") else "1"
    return get_setting(ws_id, "prayer_alerts_enabled", default_val) == "1"


def build_prayer_view(user):
    ws_id = user["active_ws"]
    city_label = get_setting(ws_id, "prayer_city_label", "الفيوم")
    city_eng = get_setting(ws_id, "prayer_city", "Faiyum")
    enabled = is_prayer_alerts_enabled(ws_id)

    timings = get_cached_prayer_times(city_eng)
    lines = [f"🕌 مواقيت الصلاة اليوم ({city_label}):\n"]

    if timings:
        lines.extend([
            f"• الفجر: {format_12h_ar(timings['Fajr'])}",
            f"• الشروق: {format_12h_ar(timings['Sunrise'])}",
            f"• الظهر: {format_12h_ar(timings['Dhuhr'])}",
            f"• العصر: {format_12h_ar(timings['Asr'])}",
            f"• المغرب: {format_12h_ar(timings['Maghrib'])}",
            f"• العشاء: {format_12h_ar(timings['Isha'])}",
        ])
        nxt = get_next_prayer_info(timings)
        if nxt:
            lines.append(f"\n⏳ الصلاة القادمة: {nxt['name']} ({nxt['time_12']}) — باقي {nxt['remaining']}")
    else:
        lines.append("⚠️ تعذر جلب المواقيت حالياً، تأكد من اسم المدينة وجرب تاني.")

    status_txt = "مُفعّلة ✅" if enabled else "متوقفة 🔕"
    lines.append(f"\n🔔 تنبيهات الأذان التلقائية: {status_txt}")

    toggle_btn = (
        {"text": "🔕 إيقاف تنبيهات الأذان", "callback_data": "toggle_prayer"}
        if enabled
        else {"text": "🔔 تفعيل تنبيهات الأذان", "callback_data": "toggle_prayer"}
    )
    kb = {
        "inline_keyboard": [
            [toggle_btn],
            [{"text": "🏙️ تغيير المحافظة / المدينة", "callback_data": "ask_prayer_city"}],
            [{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}],
        ]
    }
    return "\n".join(lines), kb


def send_user_export(chat_id, user):
    ws_id = user["active_ws"]
    admin_id = get_setting("global", "bot_admin_id")

    if str(chat_id) == str(admin_id) and not user["is_shared_mode"] and os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            tg_request(
                "sendDocument",
                payload={"chat_id": chat_id, "caption": "💾 نسخة احتياطية كاملة من قاعدة البيانات (Admin)"},
                files={"document": ("assistant_data.db", f, "application/octet-stream")},
            )
        return

    send_expenses_csv(chat_id, ws_id)


def send_expenses_csv(chat_id, ws_id):
    rows = get_all_expenses(ws_id)
    if not rows:
        send_msg(chat_id, "لا توجد مصاريف مسجلة في هذه المساحة لتصديرها حتى الآن.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "البند", "المبلغ", "التصنيف", "بواسطة", "التاريخ والوقت"])
    for r in rows:
        writer.writerow([r["id"], r["item"], r["amount"], r["category"], r["added_by"], r["created_at"]])

    csv_bytes = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    tg_request(
        "sendDocument",
        payload={"chat_id": chat_id, "caption": "📊 كشف المصاريف الكامل (جاهز للفتح في Excel)"},
        files={"document": ("expenses_report.csv", csv_bytes, "text/csv")},
    )


def build_shared_view(user):
    if not user["shared_ws"]:
        lines = [
            "👥 المساحة المشتركة (العائلة / الشغل):\n",
            "تقدر تعمل مساحة مشتركة وتبعت كود الدعوة لأي شخص عشان تسجلوا وتتابعوا المصاريف والفواتير والتذكيرات سوا، مع احتفاظ كل واحد بحسابه الشخصي المنفصل!\n",
            "اختر طريقة البدء:",
        ]
        kb = {
            "inline_keyboard": [
                [{"text": "➕ إنشاء مساحة مشتركة جديدة", "callback_data": "create_shared"}],
                [{"text": "🔑 الانضمام بكود دعوة", "callback_data": "ask_join_code"}],
                [{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}],
            ]
        }
        return "\n".join(lines), kb

    details = get_workspace_details(user["shared_ws"])
    members_names = "، ".join(m["display_name"] for m in details["members"]) if details else ""
    active_txt = "مُفعّل حالياً ✅" if user["is_shared_mode"] else "متوقف (أنت في وضعك الشخصي)"

    lines = [
        f"👨‍👩‍👧‍👦 {details['name']}:\n",
        f"• الحالة: {active_txt}",
        f"• كود الدعوة: {details['invite_code']}",
        f"• الأعضاء ({len(details['members'])}): {members_names}\n",
        "💡 ابعت الكود ده لأي شخص عشان ينضم معاك بـ: /join " + details["invite_code"],
    ]

    switch_btn = (
        {"text": "👤 التبديل لحسابي الشخصي", "callback_data": "mode_personal"}
        if user["is_shared_mode"]
        else {"text": "👨‍👩‍👧‍👦 التبديل للمساحة المشتركة", "callback_data": "mode_shared"}
    )
    kb = {
        "inline_keyboard": [
            [switch_btn],
            [{"text": "🚪 مغادرة المساحة المشتركة", "callback_data": "leave_shared"}],
            [{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}],
        ]
    }
    return "\n".join(lines), kb


def build_expenses_view(user):
    ws_id = user["active_ws"]
    s = get_expenses_summary(ws_id)
    budget = float(get_setting(ws_id, "monthly_budget", "0"))
    lines = [
        mode_badge(user),
        "💰 ملخص المصاريف:\n",
        f"• مصاريف النهاردة: {s['today']} ج.م",
        f"• مصاريف الشهر: {s['month']} ج.م",
    ]
    if budget > 0:
        rem = round(budget - s["month"], 2)
        lines.append(f"• الميزانية الشهرية: {budget} ج.م (المتبقي: {rem} ج.م)")

    if s["categories"]:
        lines.append("\n📂 التوزيع حسب الفئة:")
        for cat, val in s["categories"]:
            lines.append(f"  - {cat}: {val} ج.م")

    if s["recent"]:
        lines.append("\n🧾 آخر الحركات:")
        for r in s["recent"]:
            who = f" [{r['added_by']}]" if user["is_shared_mode"] and r.get("added_by") else ""
            lines.append(f"  • {r['item']} ({r['amount']} ج.م) — {r['category']}{who}")

    lines.append("\n💡 للإضافة السريعة ابعت نص أو فويس: غدا 120")

    kb = {
        "inline_keyboard": [
            [
                {"text": "🎯 ميزانية الشهر", "callback_data": "ask_budget"},
                {"text": "↩️ مسح آخر مصروف", "callback_data": "undo_expense"},
            ],
            [
                {"text": "📥 تصدير شيت Excel", "callback_data": "export_csv"},
                {"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"},
            ],
        ]
    }
    return "\n".join(lines), kb


def build_bills_view(user):
    ws_id = user["active_ws"]
    bills = get_bills(ws_id)
    curr_month = datetime.now(CAIRO_TZ).strftime("%Y-%m")
    lines = [mode_badge(user), "🧾 الفواتير والأقساط الشهرية الثابتة:\n"]
    buttons = []

    if not bills:
        lines.append("لا توجد فواتير أو أقساط مسجلة حالياً.")
    else:
        for b in bills:
            paid = b["last_paid_month"] == curr_month
            status = "✅ مدفوع هذا الشهر" if paid else f"⏳ ميعاد الدفع: يوم {b['due_day']}"
            lines.append(f"• {b['title']} — {b['amount']} ج.م\n  {status}")
            row_btns = []
            if not paid:
                row_btns.append({"text": f"✅ تم دفع {b['title'][:15]}", "callback_data": f"pay_bill_{b['id']}"})
            row_btns.append({"text": "🗑️ حذف", "callback_data": f"del_bill_{b['id']}"})
            buttons.append(row_btns)

    buttons.append([{"text": "➕ إضافة فاتورة / قسط شهري", "callback_data": "ask_bill"}])
    buttons.append([{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_reminders_view(user):
    ws_id = user["active_ws"]
    rems = get_upcoming_reminders(ws_id)
    lines = [mode_badge(user), "⏰ التذكيرات القادمة والمتكررة:\n"]
    buttons = []

    if not rems:
        lines.append("لا توجد تذكيرات مجدولة حالياً.")
    else:
        for r in rems:
            rep = int(r.get("repeat_seconds", 0))
            tag = " 🔁 (متكرر)" if rep > 0 else ""
            lines.append(f"• {r['text']}{tag}\n  🕒 {r['remind_at_str']}")
            buttons.append([{"text": f"❌ إلغاء: {r['text'][:28]}", "callback_data": f"del_rem_{r['id']}"}])

    lines.append("\n💡 ابعت نص أو فويس:\n• فكرني اشرب مية كل 20 دقيقة\n• فكرني أكلم أحمد بعد ساعتين")
    buttons.append([{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_gold_view(user):
    ws_id = user["active_ws"]
    gold = get_egypt_gold_prices()
    usd = get_usd_to_egp()
    alerts = get_watchers(ws_id, "gold_alert")

    lines = ["🪙 أسعار الذهب والصرف الآن في مصر:\n"]
    if gold:
        if "k24" in gold:
            lines.append(f"• عيار 24: {gold['k24']} ج.م")
        if "k21" in gold:
            lines.append(f"• عيار 21: {gold['k21']} ج.م")
        if "k18" in gold:
            lines.append(f"• عيار 18: {gold['k18']} ج.م")
        if "pound" in gold:
            lines.append(f"• الجنيه الذهب: {gold['pound']} ج.م")
    else:
        lines.append("• تعذر سحب سعر الذهب اللحظي حالياً، جرب بعد قليل.")

    if usd:
        lines.append(f"\n💵 سعر الدولار الرسمي: {usd} ج.م")

    if alerts:
        lines.append("\n🔔 التنبيهات النشطة (عيار 21):")
        for a in alerts:
            lines.append(f"  • نبهني لو نزل تحت {a['criteria']} ج.م")

    kb = {
        "inline_keyboard": [
            [{"text": "🔔 ضبط تنبيه نزول عيار 21", "callback_data": "ask_gold_alert"}],
            [{"text": "🔄 تحديث الأسعار", "callback_data": "menu_gold"}],
            [{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}],
        ]
    }
    return "\n".join(lines), kb


def build_products_view(user):
    ws_id = user["active_ws"]
    watchers = get_watchers(ws_id, "product")
    lines = [mode_badge(user), "🛒 صائد خصومات أمازون ونون:\n"]
    buttons = []

    if not watchers:
        lines.append("لا توجد منتجات مراقبة حالياً.")
    else:
        for w in watchers:
            lines.append(f"• {w['title'][:45]}\n  المستهدف: {w['criteria']} ج.م | آخر سعر: {w['last_value']} ج.م")
            buttons.append([{"text": f"🗑️ إيقاف مراقبة: {w['title'][:22]}", "callback_data": f"del_watch_{w['id']}"}])

    buttons.append([{"text": "➕ إضافة منتج للمراقبة", "callback_data": "ask_product"}])
    buttons.append([{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_jobs_view(user):
    ws_id = user["active_ws"]
    watchers = get_watchers(ws_id, "job")
    lines = [mode_badge(user), "💼 رادار الوظائف والفرص:\n"]
    buttons = []

    if not watchers:
        lines.append("لا توجد صفحات مراقبة حالياً.")
    else:
        for w in watchers:
            lines.append(f"• الكلمات: [{w['criteria']}]\n  الرابط: {w['target_url'][:45]}...")
            buttons.append([{"text": f"🗑️ حذف رادار: {w['criteria'][:24]}", "callback_data": f"del_watch_{w['id']}"}])

    buttons.append([{"text": "➕ إضافة رادار وظائف", "callback_data": "ask_job"}])
    buttons.append([{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def handle_callback(chat_id, first_name, msg_id, data):
    user = register_or_get_user(chat_id, first_name)
    ws_id = user["active_ws"]

    if data == "main_menu":
        user_states.pop(chat_id, None)
        edit_msg(chat_id, msg_id, f"{mode_badge(user)}🤖 أبو العريف جاهز 24 ساعة!\nاختر القسم أو ابعت رسالة/فويس مباشرة:", get_main_menu(user))

    elif data == "menu_prayer":
        txt, kb = build_prayer_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "toggle_prayer":
        curr = is_prayer_alerts_enabled(ws_id)
        set_setting(ws_id, "prayer_alerts_enabled", "0" if curr else "1")
        txt, kb = build_prayer_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_prayer_city":
        user_states[chat_id] = "waiting_prayer_city"
        send_msg(chat_id, "🏙️ اكتب اسم محافظتك أو مدينتك (مثال: الفيوم، القاهرة، الجيزة، الإسكندرية، المنصورة):")

    elif data == "mode_shared":
        switch_workspace_mode(chat_id, "shared")
        user = register_or_get_user(chat_id, first_name)
        edit_msg(chat_id, msg_id, f"{mode_badge(user)}✅ تم التبديل للمساحة المشتركة!\nأي مصروف أو تذكير أو حفظ هيبقى مشترك بينكم.", get_main_menu(user))

    elif data == "mode_personal":
        switch_workspace_mode(chat_id, "personal")
        user = register_or_get_user(chat_id, first_name)
        edit_msg(chat_id, msg_id, f"{mode_badge(user)}✅ تم التبديل لحسابك الشخصي المنفصل!", get_main_menu(user))

    elif data == "menu_shared":
        txt, kb = build_shared_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "create_shared":
        ws = create_shared_workspace(chat_id, f"عائلة {user['display_name']}")
        user = register_or_get_user(chat_id, first_name)
        msg = (
            f"🎉 تم إنشاء المساحة المشتركة بنجاح!\n\n"
            f"🔑 كود الدعوة: {ws['invite_code']}\n\n"
            f"ابعت الكود ده لأي شخص عشان ينضم معاك عن طريق إرسال:\n"
            f"/join {ws['invite_code']}"
        )
        edit_msg(chat_id, msg_id, msg, get_main_menu(user))

    elif data == "ask_join_code":
        user_states[chat_id] = "waiting_join_code"
        send_msg(chat_id, "🔑 اكتب كود الدعوة المكون من 6 حروف/أرقام:")

    elif data == "leave_shared":
        leave_shared_workspace(chat_id)
        user = register_or_get_user(chat_id, first_name)
        edit_msg(chat_id, msg_id, "🚪 تم الخروج من المساحة المشتركة والرجوع لحسابك الشخصي.", get_main_menu(user))

    elif data == "menu_expenses":
        txt, kb = build_expenses_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "undo_expense":
        deleted = undo_last_expense(ws_id)
        if deleted:
            send_msg(chat_id, f"↩️ تم مسح آخر مصروف:\n• {deleted['item']} ({deleted['amount']} ج.م)")
        txt, kb = build_expenses_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_budget":
        user_states[chat_id] = "waiting_budget"
        send_msg(chat_id, "💵 اكتب رقم الميزانية الشهرية بالجنيه (مثال: 8000):")

    elif data == "export_csv":
        send_expenses_csv(chat_id, ws_id)

    elif data == "menu_bills":
        txt, kb = build_bills_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_bill":
        user_states[chat_id] = "waiting_bill"
        send_msg(chat_id, "🧾 ابعت اسم الفاتورة، المبلغ، ويوم الاستحقاق في الشهر:\nمثال:\nباقة النت 250 يوم 25")

    elif data.startswith("pay_bill_"):
        bill = mark_bill_paid(ws_id, data[9:], added_by=user["display_name"])
        if bill:
            send_msg(chat_id, f"✅ تم تسجيل دفع ({bill['title']}) بمبلغ {bill['amount']} ج.م وإضافته للمصاريف.")
        txt, kb = build_bills_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data.startswith("del_bill_"):
        delete_bill(ws_id, data[9:])
        txt, kb = build_bills_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "menu_reminders":
        txt, kb = build_reminders_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data.startswith("del_rem_"):
        delete_reminder(ws_id, data[8:])
        txt, kb = build_reminders_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "menu_vault":
        tags = get_vault_tags(ws_id)
        if not tags:
            txt = f"{mode_badge(user)}🗂️ الذاكرة التانية فارغة حالياً.\n\n💡 لحفظ أي رابط أو فكرة، ابعتها ومعاها هاشتاج مثل:\nكورس بايثون ممتاز https://... #كورسات"
            kb = {"inline_keyboard": [[{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}]]}
            edit_msg(chat_id, msg_id, txt, kb)
            return

        buttons = [[{"text": "🔍 بحث في كل المحفوظات", "callback_data": "ask_vault_search"}]]
        row = []
        for tag, cnt in tags:
            row.append({"text": f"#{tag} ({cnt})", "callback_data": f"vtag_{tag[:30]}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}])
        edit_msg(chat_id, msg_id, f"{mode_badge(user)}🗂️ اختر التصنيف أو ابحث في محفوظاتك:", {"inline_keyboard": buttons})

    elif data == "ask_vault_search":
        user_states[chat_id] = "waiting_vault_search"
        send_msg(chat_id, "🔍 اكتب الكلمة اللي بتدور عليها في محفوظاتك:")

    elif data.startswith("vtag_"):
        tag = data[5:]
        items = get_vault_items_by_tag(ws_id, tag)
        lines = [f"📌 المحفوظات تحت #{tag}:\n"]
        for it in items:
            who = f" — {it['added_by']}" if user["is_shared_mode"] and it.get("added_by") else ""
            lines.append(f"• {it['content']}\n  ({it['created_at']}{who})\n")
        kb = {
            "inline_keyboard": [
                [{"text": "🔙 رجوع للتصنيفات", "callback_data": "menu_vault"}],
                [{"text": "🏠 القائمة الرئيسية", "callback_data": "main_menu"}],
            ]
        }
        edit_msg(chat_id, msg_id, "\n".join(lines), kb)

    elif data == "menu_gold":
        txt, kb = build_gold_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_gold_alert":
        user_states[chat_id] = "waiting_gold_alert"
        send_msg(chat_id, "🪙 اكتب السعر المستهدف لعيار 21 اللي تحب أنبهك لو نزل تحته (مثال: 3750):")

    elif data == "menu_products":
        txt, kb = build_products_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_product":
        user_states[chat_id] = "waiting_product"
        send_msg(chat_id, "🔗 ابعت رابط المنتج وبعده مسافة والسعر المستهدف:\nمثال:\nhttps://www.amazon.eg/dp/XXXX 1200")

    elif data == "menu_jobs":
        txt, kb = build_jobs_view(user)
        edit_msg(chat_id, msg_id, txt, kb)

    elif data == "ask_job":
        user_states[chat_id] = "waiting_job"
        send_msg(chat_id, "🔍 ابعت رابط صفحة الوظائف وبعده الكلمات المفتاحية مفصولة بفاصلة:\nمثال:\nhttps://wuzzuf.net/search/jobs?q=python python, backend, developer")

    elif data.startswith("del_watch_"):
        delete_watcher(data[10:])
        send_msg(chat_id, "🗑️ تم حذف المراقبة بنجاح.", get_main_menu(user))

    elif data == "do_backup":
        send_user_export(chat_id, user)


def handle_text_message(chat_id, first_name, text):
    user = register_or_get_user(chat_id, first_name)
    ws_id = user["active_ws"]
    clean = text.strip()
    low = clean.lower()

    if low in ("/start", "/menu", "/help"):
        user_states.pop(chat_id, None)
        welcome = (
            f"🤖 أهلاً بك يا {user['display_name']} مع أبو العريف!\n"
            f"{mode_badge(user)}\n"
            "ابعتلي نص أو فويس نوت في أي وقت:\n"
            "• مصروف: غدا 120\n"
            "• تذكير: فكرني اشرب مية كل 20 دقيقة\n"
            "• حفظ: مقال مهم https://... #شغل\n"
            "• تلخيص: ابعت رابط أي مقال لوحده\n"
            "• أو اسألني أي سؤال وهرد عليك فوراً!"
        )
        send_msg(chat_id, welcome, get_main_menu(user))
        return

    if clean in ("مواقيت الصلاة", "الصلاة", "أوقات الصلاة", "اوقات الصلاة", "/prayer"):
        txt, kb = build_prayer_view(user)
        send_msg(chat_id, txt, kb)
        return

    if low.startswith("/join "):
        code = clean.split(maxsplit=1)[1].strip()
        ws = join_shared_workspace(chat_id, code)
        if ws:
            user = register_or_get_user(chat_id, first_name)
            send_msg(chat_id, f"🎉 تم انضمامك بنجاح إلى ({ws['name']})!\nأنت الآن في الوضع المشترك وتقدر ترجع لحسابك الشخصي في أي وقت.", get_main_menu(user))
        else:
            send_msg(chat_id, "❌ كود الدعوة غير صحيح. تأكد من الحروف وجرب تاني.", get_main_menu(user))
        return

    if low == "/backup":
        send_user_export(chat_id, user)
        return

    if low.startswith("/setkey "):
        api_key = clean.split(maxsplit=1)[1].strip()
        set_setting(ws_id, "gemini_api_key", api_key)
        if str(chat_id) == str(get_setting("global", "bot_admin_id")):
            set_setting("global", "gemini_api_key", api_key)
        send_msg(chat_id, "✅ تم حفظ مفتاح Gemini API بنجاح! جرب تسألني أي حاجة أو ابعت فويس نوت.", get_main_menu(user))
        return

    if low.startswith("/search "):
        kw = clean.split(maxsplit=1)[1].strip()
        results = search_vault(ws_id, kw)
        if not results:
            send_msg(chat_id, f"🔍 ملقيتش حاجة مطابقة لـ ({kw}) في الذاكرة التانية.", get_main_menu(user))
        else:
            lines = [f"🔍 نتائج البحث عن ({kw}):\n"]
            for r in results:
                lines.append(f"• [#{r['tag']}] {r['content']}\n  ({r['created_at']})")
            send_msg(chat_id, "\n".join(lines), get_main_menu(user))
        return

    # 1. معالجة الخطوات التفاعلية المنتظرة
    state = user_states.get(chat_id)
    if state == "waiting_prayer_city":
        user_states.pop(chat_id, None)
        eng_city = resolve_egypt_city(clean)
        timings = get_cached_prayer_times(eng_city)
        if timings:
            set_setting(ws_id, "prayer_city", eng_city)
            set_setting(ws_id, "prayer_city_label", clean)
            txt, kb = build_prayer_view(user)
            send_msg(chat_id, f"✅ تم تحديث المدينة إلى ({clean}) بنجاح!\n\n{txt}", kb)
        else:
            send_msg(chat_id, "❌ ملقيتش مواقيت للمدينة دي، جرب تكتب اسم المحافظة بشكل أوضح (مثلاً: القاهرة أو الفيوم).", get_main_menu(user))
        return

    if state == "waiting_join_code":
        user_states.pop(chat_id, None)
        handle_text_message(chat_id, first_name, f"/join {clean}")
        return

    if state == "waiting_budget":
        user_states.pop(chat_id, None)
        try:
            val = float(normalize_digits(clean))
            if val <= 0:
                raise ValueError
            set_setting(ws_id, "monthly_budget", val)
            send_msg(chat_id, f"✅ تم تحديد الميزانية الشهرية بمبلغ {val} ج.م بنجاح.", get_main_menu(user))
        except ValueError:
            send_msg(chat_id, "❌ أدخل رقماً صحيحاً للميزانية.", get_main_menu(user))
        return

    if state == "waiting_bill":
        user_states.pop(chat_id, None)
        norm = normalize_digits(clean)
        m_day = re.search(r"يوم\s+(\d{1,2})", norm)
        nums = re.findall(r"\d+(?:\.\d+)?", re.sub(r"يوم\s+\d{1,2}", "", norm))
        if m_day and nums:
            due_day = int(m_day.group(1))
            amount = float(nums[-1])
            title = re.sub(r"يوم\s+\d{1,2}|\d+(?:\.\d+)?", "", norm).strip(" ،-:") or "فاتورة شهرية"
            add_bill(ws_id, title, amount, due_day)
            send_msg(chat_id, f"✅ تم حفظ الفاتورة:\n• {title}: {amount} ج.م (يوم {due_day} من كل شهر)", get_main_menu(user))
        else:
            send_msg(chat_id, "❌ الصيغة غير صحيحة. اكتب مثال: باقة النت 250 يوم 25", get_main_menu(user))
        return

    if state == "waiting_vault_search":
        user_states.pop(chat_id, None)
        handle_text_message(chat_id, first_name, f"/search {clean}")
        return

    if state == "waiting_gold_alert":
        user_states.pop(chat_id, None)
        try:
            target = float(normalize_digits(clean))
            if target <= 0:
                raise ValueError
            add_watcher(ws_id, "gold_alert", "", target, title="تنبيه عيار 21")
            send_msg(chat_id, f"🔔 تم تفعيل التنبيه! هبعتلك رسالة فورية أول ما عيار 21 ينزل عن {target} ج.م.", get_main_menu(user))
        except ValueError:
            send_msg(chat_id, "❌ أدخل سعراً صحيحاً.", get_main_menu(user))
        return

    if state == "waiting_product":
        user_states.pop(chat_id, None)
        parts = normalize_digits(clean).split()
        if len(parts) >= 2 and parts[0].startswith("http"):
            url = parts[0]
            try:
                target_price = float(parts[-1])
                send_msg(chat_id, "⏳ جاري فحص سعر المنتج الحالي...")
                info = check_product_price(url)
                curr = info["price"] if info and info["price"] else "غير محدد"
                title = info["title"] if info else "منتج"
                add_watcher(ws_id, "product", url, target_price, last_value=curr, title=title)
                send_msg(
                    chat_id,
                    f"✅ تمت إضافة المنتج للمراقبة:\n• {title}\n• السعر الحالي: {curr} ج.م\n• السعر المستهدف: {target_price} ج.م",
                    get_main_menu(user),
                )
                return
            except ValueError:
                pass
        send_msg(chat_id, "❌ الصيغة غير صحيحة. ابعت الرابط وبعده مسافة والسعر المستهدف.", get_main_menu(user))
        return

    if state == "waiting_job":
        user_states.pop(chat_id, None)
        parts = clean.split(maxsplit=1)
        if len(parts) == 2 and parts[0].startswith("http"):
            url, keywords = parts[0], parts[1]
            add_watcher(ws_id, "job", url, keywords, title=keywords)
            for _, link in scan_jobs_page(url, keywords):
                is_link_seen(ws_id, link)
            send_msg(chat_id, f"✅ تم تشغيل رادار الوظائف بنجاح!\nهبعتلك أي إعلان جديد يطابق: [{keywords}]", get_main_menu(user))
            return
        send_msg(chat_id, "❌ الصيغة غير صحيحة. ابعت الرابط وبعده مسافة والكلمات المفتاحية.", get_main_menu(user))
        return

    # 2. التحقق من التذكير الطبيعي
    rem = parse_reminder(clean)
    if rem:
        rep_sec = rem.get("repeat_seconds", 0)
        dt_str = add_reminder(ws_id, rem["text"], rem["remind_dt"], rep_sec)
        if rep_sec > 0:
            msg = (
                f"🔁 تم ضبط تذكير متكرر!\n"
                f"• المهمة: {rem['text']}\n"
                f"• التكرار: {rem['repeat_label']}\n"
                f"• أول تنبيه: {dt_str}"
            )
        else:
            msg = f"⏰ تم ضبط التذكير!\n• المهمة: {rem['text']}\n• الموعد: {dt_str}"
        send_msg(chat_id, msg, get_main_menu(user))
        return

    # 3. التحقق من الذاكرة التانية (#هاشتاج)
    vault_item = parse_vault_message(clean)
    if vault_item:
        add_vault_item(ws_id, vault_item["tag"], vault_item["content"], added_by=user["display_name"])
        send_msg(chat_id, f"🗂️ تم الحفظ في الذاكرة التانية تحت تصنيف #{vault_item['tag']}", get_main_menu(user))
        return

    # 4. التحقق من المصاريف
    exp = parse_expense(clean)
    if exp:
        add_expense(ws_id, exp["item"], exp["amount"], exp["category"], added_by=user["display_name"])
        s = get_expenses_summary(ws_id)
        send_msg(
            chat_id,
            f"✅ تم تسجيل المصروف:\n• {exp['item']}: {exp['amount']} ج.م ({exp['category']})\n📊 إجمالي مصاريف النهاردة: {s['today']} ج.م",
            get_main_menu(user),
        )
        return

    # 5. تلخيص رابط مقال بالذكاء الاصطناعي
    standalone_url = extract_standalone_url(clean)
    if standalone_url:
        send_msg(chat_id, "⏳ جاري قراءة الرابط وتلخيصه...")
        summary = summarize_article_url(standalone_url, ws_id=ws_id)
        send_msg(chat_id, summary, get_main_menu(user))
        return

    # 6. مخ Gemini لأي سؤال أو محادثة عامة
    reply = ask_gemini_chat(clean, ws_id=ws_id)
    send_msg(chat_id, reply, get_main_menu(user))


def handle_voice_message(chat_id, first_name, voice_obj):
    user = register_or_get_user(chat_id, first_name)
    file_id = voice_obj.get("file_id")
    if not file_id:
        return
    file_info = tg_request("getFile", {"file_id": file_id})
    file_path = file_info.get("result", {}).get("file_path")
    if not file_path:
        send_msg(chat_id, "❌ تعذر تحميل الرسالة الصوتية.")
        return

    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    ogg_bytes = requests.get(audio_url, timeout=20).content

    transcribed, err = transcribe_voice_note(ogg_bytes, ws_id=user["active_ws"])
    if err:
        send_msg(chat_id, err)
        return
    if not transcribed:
        send_msg(chat_id, "🎙️ الصوت مكانش واضح، جرب تسجلها تاني.")
        return

    send_msg(chat_id, f"🎙️ سمعتك بتقول: «{transcribed}»")
    handle_text_message(chat_id, first_name, transcribed)


def notify_workspace(ws_id, text, markup=None):
    for cid in get_workspace_recipients(ws_id):
        send_msg(cid, text, markup)


def check_and_send_prayer_alerts(now):
    """فحص وإرسال تنبيهات الأذان في وقتها بدقة"""
    today_str = now.strftime("%Y-%m-%d")
    curr_hhmm = now.strftime("%H:%M")
    prayers_ar = {
        "Fajr": "الفجر",
        "Dhuhr": "الظهر",
        "Asr": "العصر",
        "Maghrib": "المغرب",
        "Isha": "العشاء",
    }

    for ws_id in get_all_active_workspaces():
        if not is_prayer_alerts_enabled(ws_id):
            continue

        city_eng = get_setting(ws_id, "prayer_city", "Faiyum")
        city_label = get_setting(ws_id, "prayer_city_label", "الفيوم")
        timings = get_cached_prayer_times(city_eng)
        if not timings:
            continue

        for p_key, p_name in prayers_ar.items():
            if timings.get(p_key) == curr_hhmm:
                sent_flag = f"{today_str}_{p_key}"
                if get_setting(ws_id, "last_prayer_sent") != sent_flag:
                    set_setting(ws_id, "last_prayer_sent", sent_flag)
                    msg = (
                        f"🕌 حان الآن موعد أذان {p_name}\n"
                        f"🕒 الوقت: {format_12h_ar(curr_hhmm)} بتوقيت {city_label}\n"
                        "🤲 تقبل الله طاعتكم"
                    )
                    notify_workspace(ws_id, msg)


def send_morning_briefing(ws_id, weather, gold, usd):
    today_rems = get_today_reminders(ws_id)
    due_bills = get_due_bills(ws_id, days_ahead=2)
    is_shared = ws_id.startswith("shared_")

    if is_shared and not today_rems and not due_bills:
        return

    title = "☀️ صباح الفل! النشرة المشتركة للعائلة:\n" if is_shared else "☀️ صباح الفل! نشرتك اليومية من أبو العريف:\n"
    lines = [title]
    if not is_shared:
        if weather:
            lines.append(f"🌤️ حالة الجو: {weather}")
        if gold and "k21" in gold:
            lines.append(f"🪙 الذهب عيار 21: {gold['k21']} ج.م")
        if usd:
            lines.append(f"💵 الدولار: {usd} ج.م")

    if today_rems:
        lines.append("\n⏰ تذكيرات النهاردة:")
        for r in today_rems:
            lines.append(f"  • {r['text']} ({r['remind_at_str'].split(' ', 1)[1]})")

    buttons = []
    if due_bills:
        lines.append("\n🧾 فواتير وأقساط قرب ميعادها:")
        for b in due_bills:
            lines.append(f"  • {b['title']}: {b['amount']} ج.م (يوم {b['due_day']})")
            buttons.append([{"text": f"✅ تم دفع {b['title'][:20]}", "callback_data": f"pay_bill_{b['id']}"}])

    buttons.append([{"text": "🏠 القائمة الرئيسية", "callback_data": "main_menu"}])
    notify_workspace(ws_id, "\n".join(lines), {"inline_keyboard": buttons})


def send_evening_wrapup(ws_id):
    now = datetime.now(CAIRO_TZ)
    s = get_expenses_summary(ws_id)
    budget = float(get_setting(ws_id, "monthly_budget", "0"))
    is_shared = ws_id.startswith("shared_")

    if is_shared and s["today"] == 0 and budget == 0:
        return

    header = "🌙 جرد المساحة المشتركة آخر اليوم:\n" if is_shared else "🌙 جرد آخر اليوم:\n"
    lines = [
        header,
        f"• مصاريف النهاردة: {s['today']} ج.م",
        f"• إجمالي مصاريف الشهر: {s['month']} ج.م",
    ]
    if budget > 0:
        rem = round(budget - s["month"], 2)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        days_left = max(days_in_month - now.day, 1)
        safe_daily = round(max(rem, 0) / days_left, 2)
        lines.append(f"• المتبقي من الميزانية: {rem} ج.م")
        lines.append(f"• الحد اليومي الآمن لباقي الشهر: {safe_daily} ج.م/يوم")

    notify_workspace(ws_id, "\n".join(lines))


def background_scheduler():
    tick = 0
    while True:
        now = datetime.now(CAIRO_TZ)
        today_str = now.strftime("%Y-%m-%d")

        # 1. التذكيرات المستحقة (كل 10 ثواني)
        try:
            for r in get_due_reminders():
                rep = int(r.get("repeat_seconds", 0))
                is_shared = r["ws_id"].startswith("shared_")
                prefix = "👨‍👩‍👧‍👦 " if is_shared else ""
                header = f"{prefix}🔁 تذكير متكرر:" if rep > 0 else f"{prefix}⏰ تذكير حان موعده الآن:"
                notify_workspace(r["ws_id"], f"{header}\n\n📌 {r['text']}")
        except Exception:
            pass

        # 2. تنبيهات مواقيت الصلاة (كل 10 ثواني)
        try:
            check_and_send_prayer_alerts(now)
        except Exception:
            pass

        # 3. نشرة الصباح (9:00 صباحاً) وجرد المساء (11:00 مساءً)
        try:
            if now.hour == 9 and get_setting("global", "last_morning_brief") != today_str:
                set_setting("global", "last_morning_brief", today_str)
                weather = get_weather_info()
                gold = get_egypt_gold_prices()
                usd = get_usd_to_egp()
                for ws_id in get_all_active_workspaces():
                    send_morning_briefing(ws_id, weather, gold, usd)

            elif now.hour == 23 and get_setting("global", "last_evening_brief") != today_str:
                set_setting("global", "last_evening_brief", today_str)
                for ws_id in get_all_active_workspaces():
                    send_evening_wrapup(ws_id)

            elif now.weekday() == 4 and now.hour == 14 and get_setting("global", "last_weekly_backup") != today_str:
                set_setting("global", "last_weekly_backup", today_str)
                admin_id = get_setting("global", "bot_admin_id")
                if admin_id and os.path.exists(DB_PATH):
                    with open(DB_PATH, "rb") as f:
                        tg_request(
                            "sendDocument",
                            payload={"chat_id": admin_id, "caption": "💾 النسخة الاحتياطية الأسبوعية التلقائية"},
                            files={"document": ("assistant_data.db", f, "application/octet-stream")},
                        )
        except Exception:
            pass

        # 4. مراقبة سعر الذهب (كل 30 دقيقة = 180 دورة)
        if tick % 180 == 0:
            try:
                gold_alerts = get_watchers(w_type="gold_alert")
                if gold_alerts:
                    gold = get_egypt_gold_prices()
                    if gold and "k21" in gold:
                        curr_21 = gold["k21"]
                        for a in gold_alerts:
                            target = float(a["criteria"])
                            if curr_21 <= target:
                                notify_workspace(a["ws_id"], f"🚨 تنبيه سعر الذهب!\nعيار 21 وصل الآن إلى {curr_21} ج.م (أقل من هدفك {target} ج.م).")
                                delete_watcher(a["id"])
            except Exception:
                pass

        # 5. مراقبة المنتجات والوظائف (كل 60 دقيقة = 360 دورة)
        if tick % 360 == 0:
            try:
                for p in get_watchers(w_type="product"):
                    info = check_product_price(p["target_url"])
                    if info and info["price"] is not None:
                        curr_price = info["price"]
                        target_price = float(p["criteria"])
                        if curr_price <= target_price:
                            notify_workspace(
                                p["ws_id"],
                                f"🔥 عرض وتخفيض!\nالمنتج: {p['title']}\nنزل سعره إلى {curr_price} ج.م (هدفك: {target_price} ج.م)\n🔗 {p['target_url']}",
                            )
                            delete_watcher(p["id"])
            except Exception:
                pass

            try:
                for j in get_watchers(w_type="job"):
                    new_matches = scan_jobs_page(j["target_url"], j["criteria"])
                    for title, link in new_matches:
                        if not is_link_seen(j["ws_id"], link):
                            notify_workspace(j["ws_id"], f"💼 فرصة جديدة مطابقة لاهتمامك [{j['criteria']}]:\n\n• {title}\n🔗 {link}")
            except Exception:
                pass

        tick += 1
        time.sleep(10)


def telegram_polling_loop():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            res = requests.get(
                url,
                params={"offset": offset, "timeout": 20, "allowed_updates": '["message", "callback_query"]'},
                timeout=25,
            ).json()

            if not res.get("ok"):
                time.sleep(5)
                continue

            for upd in res.get("result", []):
                offset = upd["update_id"] + 1

                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    tg_request("answerCallbackQuery", {"callback_query_id": cb["id"]})
                    msg = cb.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    first_name = cb.get("from", {}).get("first_name", "مستخدم")
                    handle_callback(chat_id, first_name, msg.get("message_id"), cb.get("data", ""))

                elif "message" in upd:
                    msg = upd["message"]
                    chat_id = str(msg["chat"]["id"])
                    first_name = msg.get("from", {}).get("first_name", "مستخدم")

                    if "voice" in msg:
                        threading.Thread(target=handle_voice_message, args=(chat_id, first_name, msg["voice"]), daemon=True).start()
                    elif "text" in msg:
                        handle_text_message(chat_id, first_name, msg["text"])

        except Exception as e:
            print("Polling error:", e, flush=True)
            time.sleep(5)


app = Flask(__name__)


@app.route("/")
def health():
    return "Abo El-Areef Bot is running 24/7!", 200


if __name__ == "__main__":
    init_db()
    print("🚀 Abo El-Areef Bot started...", flush=True)
    threading.Thread(target=background_scheduler, daemon=True).start()
    threading.Thread(target=telegram_polling_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)