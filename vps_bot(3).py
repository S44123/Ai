

from flask import Flask
import threading
import json
import sys
import time
import uuid
import argparse
import requests

BASE = "https://android.chat.openai.com"
UA = "ChatGPT/1.2026.293 (Android 10; unknown; build 2629319)"
FREE_MODELS = ["auto", "gpt-5-6", "gpt-5-6-mini", "gpt-6", "gpt-6-mini"]

G = "\033[92m"; C = "\033[96m"; Y = "\033[93m"; R = "\033[91m"; D = "\033[90m"; B = "\033[1m"; X = "\033[0m"


class CGPT:
    def __init__(self, token=None, device_id=None, account_id=None, proxy=None):
        self.token = token
        self.device_id = device_id or str(uuid.uuid4())
        self.account_id = account_id or "default"
        self.session_id = str(uuid.uuid4())
        self.conversation_id = None
        self.parent_id = None
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def _headers(self, target_path, stream=False):
        h = {
            "user-agent": UA,
            "oai-package-name": "com.openai.chatgpt",
            "x-openai-target-path": target_path,
            "oai-client-type": "android",
            "oai-device-id": self.device_id,
            "x-device-tier": "flagship",
            "chatgpt-account-id": self.account_id,
            "chatgpt-residency-region": "no_constraint",
            "accept-language": "ar-SA,ar;q=0.9",
            "accept": "text/event-stream,application/json" if stream else "application/json",
            "content-type": "application/json",
            "oai-client-session": json.dumps({
                "client_session_id": str(uuid.uuid4()),
                "client_session_start": int(time.time() * 1000),
            }),
        }
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        return h

    def _api(self, path, body=None, method=None):
        prefix = "/backend-anon" if not self.token else "/backend-api"
        url = BASE + prefix + path
        hdrs = self._headers(prefix + path)
        if method == "GET" or body is None:
            return self.s.get(url, headers=hdrs, timeout=30)
        return self.s.post(url, headers=hdrs, json=body, timeout=60)

    def prepare(self):
        body = {
            "conversation_id": self.conversation_id,
            "action": "next",
            "parent_message_id": self.parent_id,
            "messages": [],
            "model": "auto",
            "supported_encodings": ["v1"],
            "supports_buffering": True,
            "timezone": "Asia/Baghdad",
        }
        body = {k: v for k, v in body.items() if v is not None}
        try:
            r = self._api("/f/conversation/prepare", body)
            if r.status_code == 200:
                return r.json().get("conduit_token")
        except Exception:
            pass
        return None

    def _msg(self, text):
        return {
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
            "content": {"parts": [text], "content_type": "text"},
            "status": "finished_successfully",
            "recipient": "all",
            "metadata": {
                "model_slug": "auto", "default_model_slug": "auto",
                "is_visually_hidden_from_conversation": False,
                "exclude_after_next_user_message": False,
                "content_references": [], "search_result_groups": [],
                "search_queries": [], "submission_mode": "manual_send",
                "image_results": [], "real_time_audio_has_video": False,
                "system_hints": [], "dictation": False,
                "voice_mode_message": False, "trigger_async_ux": False,
                "writing_blocks": {},
            },
        }

    def new_chat(self):
        self.conversation_id = None
        self.parent_id = None
        self.session_id = str(uuid.uuid4())

    def chat(self, text, model="auto", on_delta=None, retries=2):
        last_err = None
        for attempt in range(retries + 1):
            res = self._chat_once(text, model, on_delta)
            if res["ok"] and res["text"]:
                return res
            last_err = res
            if res.get("status") in (401, 403):
                break
            time.sleep(1.5)
        return last_err or {"ok": False, "status": 0, "body": "خطأ غير معروف"}

    def _chat_once(self, text, model, on_delta):
        conduit = self.prepare()
        body = {
            "action": "next",
            "messages": [self._msg(text)],
            "model": model,
            "supported_encodings": ["v1"],
            "supports_buffering": True,
            "timezone": "Asia/Baghdad",
        }
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        if self.parent_id:
            body["parent_message_id"] = self.parent_id

        prefix = "/backend-anon" if not self.token else "/backend-api"
        hdrs = self._headers(prefix + "/f/conversation", stream=True)
        hdrs["oai-session-id"] = self.session_id
        hdrs["x-oai-convo-session-id"] = self.session_id
        hdrs["x-oai-turn-trace-id"] = str(uuid.uuid4())
        hdrs["x-openai-web-sse-compression"] = "gzip"
        hdrs["cache-control"] = "no-cache"
        hdrs["x-oai-library-attachments-format"] = "unified-v1"
        if conduit:
            hdrs["x-conduit-token"] = conduit

        try:
            r = self.s.post(BASE + prefix + "/f/conversation", headers=hdrs,
                            json=body, timeout=180, stream=True)
        except Exception as e:
            return {"ok": False, "status": 0, "body": str(e)[:200]}
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "body": r.text[:300]}

        full = ""
        result = {"ok": True, "text": "", "conversation_id": None, "message_id": None}

        def handle_ops(ops):
            nonlocal full
            for op in ops:
                if not isinstance(op, dict):
                    continue
                p, v, o = op.get("p", ""), op.get("v"), op.get("o")
                if o == "append" and "/parts/" in p and isinstance(v, str):
                    full += v
                    if on_delta:
                        on_delta(v)
                elif o in ("set", "replace") and "/parts/" in p and isinstance(v, str):
                    full = v

        def handle_msg(msg):
            nonlocal full
            if not isinstance(msg, dict):
                return
            if (msg.get("author") or {}).get("role") == "assistant":
                if msg.get("id"):
                    result["message_id"] = msg["id"]
                parts = (msg.get("content") or {}).get("parts") or []
                joined = "".join(x for x in parts if isinstance(x, str))
                if joined and len(joined) >= len(full):
                    full = joined

        try:
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                if isinstance(ev, str):
                    continue
                if isinstance(ev, list):
                    handle_ops(ev)
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("conversation_id"):
                    result["conversation_id"] = ev["conversation_id"]
                    self.conversation_id = ev["conversation_id"]
                v = ev.get("v")
                if isinstance(v, list):
                    handle_ops(v)
                elif isinstance(v, dict) and "message" in v:
                    handle_msg(v["message"])
                elif isinstance(v, str) and ev.get("o") in ("append", "set", "replace"):
                    handle_ops([ev])
                if isinstance(ev.get("message"), dict):
                    handle_msg(ev["message"])
        except Exception as e:
            if not full:
                return {"ok": False, "status": 0, "body": str(e)[:200]}

        if result["message_id"]:
            self.parent_id = result["message_id"]
        result["text"] = full
        return result

    def models(self):
        try:
            r = self._api("/models?iim=false", method="GET")
            if r.status_code == 200:
                return [m.get("slug") for m in r.json().get("models", [])]
        except Exception:
            pass
        return []


# ═══════════════════════════════════════════════════════════════════
#                         Telegram Bot layer
#   pip install pyTelegramBotAPI requests
#   export TELEGRAM_BOT_TOKEN="123456:ABC..."   (من BotFather)
#   python vps_bot.py
# ═══════════════════════════════════════════════════════════════════
import os
import threading
import hashlib
import tempfile
try:
    import fcntl          # متوفر على لينكس / ترمكس
except ImportError:
    fcntl = None

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8817740027:AAHBMnD16A4sR8E02_84AE2_5xkcFZdrQ9M").strip()
CGPT_TOKEN = os.environ.get("CGPT_TOKEN") or None      # اختياري (بدونه مجاني)
CGPT_PROXY = os.environ.get("CGPT_PROXY") or None      # اختياري
# اختياري: قصر البوت على مستخدمين محددين  ALLOWED_USERS="111,222"
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x.isdigit()}

TG_LIMIT = 4000          # حد تيليقرام 4096 حرف للرسالة
EDIT_EVERY = 1.5         # أقل فاصل (ثواني) بين تعديلات البث
SESSION_TTL = 6 * 3600   # تنظيف الجلسات الخاملة بعد 6 ساعات

if not BOT_TOKEN:
    sys.exit("حدّد توكن البوت أولاً:  export TELEGRAM_BOT_TOKEN='...'")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True, num_threads=20)

HELP_TEXT = (
    "🚀 ChatGPT Free — بدون حساب\n\n"
    "أرسل أي سؤال وسأرد عليك، والذاكرة مفعّلة داخل المحادثة.\n\n"
    "الأوامر:\n"
    "/new — محادثة جديدة (تصفير الذاكرة)\n"
    "/model — اختيار النموذج\n"
    "/help — هذه الرسالة\n\n"
    "Developer: @ZOROTOP2 — All rights reserved"
)

# ───────────────────────── الجلسات (جلسة لكل محادثة) ─────────────────────────
_sessions = {}
_sessions_lock = threading.Lock()


def get_session(chat_id):
    now = time.time()
    with _sessions_lock:
        for k in [k for k, v in _sessions.items()
                  if now - v["seen"] > SESSION_TTL and not v["lock"].locked()]:
            del _sessions[k]
        s = _sessions.get(chat_id)
        if s is None:
            s = {
                "cg": CGPT(token=CGPT_TOKEN, proxy=CGPT_PROXY),
                "model": "auto",
                "lock": threading.Lock(),
                "seen": now,
            }
            _sessions[chat_id] = s
        s["seen"] = now
        return s


def allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


# ───────────────────────── أدوات مساعدة ─────────────────────────
def tg(fn, *args, **kwargs):
    """استدعاء تيليقرام مع إعادة المحاولة عند 429 وتجاهل 'message is not modified'."""
    for _ in range(3):
        try:
            return fn(*args, **kwargs)
        except ApiTelegramException as e:
            if "not modified" in str(e):
                return None
            if getattr(e, "error_code", 0) == 429:
                params = (getattr(e, "result_json", None) or {}).get("parameters") or {}
                time.sleep(int(params.get("retry_after", 2)) + 1)
                continue
            raise
    return None


def split_text(text, limit=TG_LIMIT):
    parts = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts or ["…"]


def model_keyboard(current):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        types.InlineKeyboardButton(("✅ " if n == current else "") + n, callback_data="model:" + n)
        for n in FREE_MODELS
    ])
    return kb


# ───────────────────────── الأوامر ─────────────────────────
@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    if not allowed(m.from_user.id):
        return bot.reply_to(m, "⛔ غير مصرّح لك باستخدام هذا البوت")
    bot.reply_to(m, HELP_TEXT)


@bot.message_handler(commands=["new"])
def cmd_new(m):
    if not allowed(m.from_user.id):
        return
    s = get_session(m.chat.id)
    if s["lock"].locked():
        return bot.reply_to(m, "⏳ انتظر حتى ينتهي الرد الحالي ثم أعد المحاولة")
    s["cg"].new_chat()
    bot.reply_to(m, "✦ محادثة جديدة — الذاكرة صُفّرت")


@bot.message_handler(commands=["model", "models"])
def cmd_model(m):
    if not allowed(m.from_user.id):
        return
    s = get_session(m.chat.id)
    parts = m.text.split(None, 1)
    if len(parts) == 2:                       # /model gpt-6
        name = parts[1].strip()
        if name in FREE_MODELS:
            s["model"] = name
            return bot.reply_to(m, f"✦ النموذج الآن: {name}")
        return bot.reply_to(m, "نموذج غير معروف. المتاح: " + " | ".join(FREE_MODELS))
    bot.reply_to(m, "اختر النموذج:", reply_markup=model_keyboard(s["model"]))


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("model:"))
def cb_model(c):
    if not allowed(c.from_user.id):
        return bot.answer_callback_query(c.id)
    name = c.data.split(":", 1)[1]
    if name not in FREE_MODELS:
        return bot.answer_callback_query(c.id, "نموذج غير معروف")
    s = get_session(c.message.chat.id)
    s["model"] = name
    bot.answer_callback_query(c.id, f"النموذج الآن: {name}")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id,
                                      reply_markup=model_keyboard(name))
    except Exception:
        pass


# ───────────────────────── الرد على الرسائل ─────────────────────────
def answer(m, s):
    chat_id = m.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass
    msg = bot.send_message(chat_id, "⏳ جاري التفكير…", reply_to_message_id=m.message_id)
    st = {"buf": "", "t": 0.0, "shown": ""}

    def on_delta(d):                           # بث مباشر عبر تعديل الرسالة
        st["buf"] += d
        now = time.time()
        if now - st["t"] < EDIT_EVERY:
            return
        st["t"] = now
        preview = st["buf"][:TG_LIMIT] + " ▌"
        if preview == st["shown"]:
            return
        try:
            tg(bot.edit_message_text, preview, chat_id, msg.message_id)
            st["shown"] = preview
        except Exception:
            pass

    res = s["cg"].chat(m.text, model=s["model"], on_delta=on_delta)

    if res.get("ok") and res.get("text"):
        chunks = split_text(res["text"])
        try:
            tg(bot.edit_message_text, chunks[0], chat_id, msg.message_id)
        except Exception:
            tg(bot.send_message, chat_id, chunks[0])
        for extra in chunks[1:]:
            tg(bot.send_message, chat_id, extra)
    else:
        err = (f"⚠️ تعثر الطلب (status={res.get('status')})\n"
               f"{str(res.get('body', ''))[:200]}\n\n"
               "جرّب مرة أخرى أو أرسل /new")
        try:
            tg(bot.edit_message_text, err, chat_id, msg.message_id)
        except Exception:
            tg(bot.send_message, chat_id, err)


@bot.message_handler(content_types=["text"],
                     func=lambda m: bool(m.text) and not m.text.startswith("/"))
def on_text(m):
    if not allowed(m.from_user.id):
        return bot.reply_to(m, "⛔ غير مصرّح لك باستخدام هذا البوت")
    s = get_session(m.chat.id)
    if not s["lock"].acquire(blocking=False):
        return bot.reply_to(m, "⏳ انتظر حتى ينتهي الرد السابق")
    try:
        answer(m, s)
    except Exception as e:
        try:
            bot.send_message(m.chat.id, f"⚠️ خطأ غير متوقع: {str(e)[:150]}")
        except Exception:
            pass
    finally:
        s["lock"].release()


@bot.message_handler(content_types=["photo", "voice", "audio", "video", "document", "sticker"])
def on_other(m):
    bot.reply_to(m, "أرسل نصاً فقط من فضلك 🙂")


CONFLICT_MSG = (
    "\n✖ خطأ 409: توجد نسخة أخرى من البوت تعمل الآن بنفس التوكن.\n"
    "  • أغلق نوافذ ترمكس الأخرى، أو نفّذ:  pkill -f vps\n"
    "  • إن بقي الخطأ فالتوكن مستخدم في مكان آخر (جهاز/سيرفر/مشروع بوت ثاني)؛\n"
    "    أوقفه هناك، أو أرسل /revoke إلى BotFather لتوليد توكن جديد وغيّره في الملف."
)


def acquire_instance_lock():
    """يمنع تشغيل نسختين من البوت على نفس الجهاز."""
    if fcntl is None:
        return None
    name = "vps_bot_" + hashlib.md5(BOT_TOKEN.encode()).hexdigest()[:10] + ".lock"
    f = open(os.path.join(tempfile.gettempdir(), name), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"{R}✖ البوت يعمل بالفعل في نافذة/عملية أخرى على هذا الجهاز.\n"
                 f"  أغلقها أولاً أو نفّذ:  pkill -f vps   ثم أعد التشغيل.{X}")
    f.write(str(os.getpid()))
    f.flush()
    return f


def run_polling():
    skipped = False
    while True:
        try:
            if not skipped:                    # تجاهل الرسائل القديمة المتراكمة
                ups = bot.get_updates(offset=-1, timeout=10, long_polling_timeout=0)
                if ups:
                    bot.get_updates(offset=ups[-1].update_id + 1,
                                    timeout=10, long_polling_timeout=0)
                skipped = True
            bot.polling(non_stop=False, timeout=30, long_polling_timeout=30)
        except KeyboardInterrupt:
            print(f"\n{C}إلى اللقاء 👋{X}")
            return
        except ApiTelegramException as e:
            if getattr(e, "error_code", 0) == 409:
                sys.exit(f"{R}{CONFLICT_MSG}{X}")
            print(f"{R}تيليقرام: {str(e)[:150]} — إعادة المحاولة بعد 5 ثوان{X}")
            time.sleep(5)
        except Exception as e:                 # انقطاع الشبكة وما شابه
            print(f"{R}انقطاع اتصال: {str(e)[:100]} — إعادة المحاولة بعد 5 ثوان{X}")
            time.sleep(5)


def main():
    _lock = acquire_instance_lock()            # يبقى مفتوحاً طوال التشغيل
    try:
        me = bot.get_me()
    except ApiTelegramException as e:
        sys.exit(f"{R}✖ توكن البوت غير صحيح أو ملغى: {str(e)[:150]}{X}")
    except Exception as e:
        sys.exit(f"{R}✖ تعذّر الاتصال بتيليقرام: {str(e)[:150]}{X}")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_my_commands([
            types.BotCommand("new", "محادثة جديدة"),
            types.BotCommand("model", "اختيار النموذج"),
            types.BotCommand("help", "المساعدة"),
        ])
    except Exception:
        pass
        print(f"{G}{B}✔ البوت يعمل: {me.username}{X}")
    print(f"{D}Developer: @ZOROTOP2 — All rights reserved{X}")

    # أضف أوامر الفلاسك هنا فوق تشغيل البوت:
    app = Flask("")

    @app.route("/")
    def home():
        return "Bot is active and running 24/7!"

    def run_web():
        app.run(host="0.0.0.0", port=8080)

    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()

    run_polling()


if __name__ == "__main__":
    main()
