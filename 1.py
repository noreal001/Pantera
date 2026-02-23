import logging
import os
import traceback
import aiohttp
import asyncio
import httpx
import sys
import uvicorn
import json
import base64
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TOKEN = os.getenv('TOKEN')
BASE_WEBHOOK_URL = os.getenv('WEBHOOK_BASE_URL')
WEBHOOK_PATH = "/webhook/ai-bear-123456"
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://snwbavhrnjpowuezrtyk.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

CONFIG_FILE = "bot_config.json"
DEFAULT_CONFIG = {
    "model": "gemini-3-flash-preview",
    "temperature": 0.7,
    "thinking_budget": 1024
}

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
    except Exception as e:
        logger.error(f"Config load error: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Config save error: {e}")
        return False

# --- FastAPI app ---
app = FastAPI()

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception handler: {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# --- Импорт системы контекста ---
try:
    from context import add_user_message, add_assistant_message, get_user_context, clear_user_context
    CONTEXT_ENABLED = True
    logger.info("Система контекста загружена")
except ImportError:
    CONTEXT_ENABLED = False
    logger.info("Система контекста недоступна")

# --- Загрузка данных BAHUR ---
def load_bahur_data():
    data_dir = "bahur_data"
    combined_data = ""
    try:
        if not os.path.exists(data_dir):
            logger.warning(f"Папка {data_dir} не найдена")
            return ""
        for filename in sorted(os.listdir(data_dir)):
            if filename.endswith('.txt'):
                file_path = os.path.join(data_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        combined_data += f"\n\n=== {filename} ===\n{f.read()}\n"
                except Exception as e:
                    logger.error(f"Ошибка чтения {filename}: {e}")
        return combined_data
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")
        return ""

BAHUR_DATA = load_bahur_data()

# --- Системный промпт AI-Пантеры ---
SYSTEM_PROMPT = """ПРОФИЛЬ И ХАРАКТЕР:
Ты — AI-Пантера, информационный помощник компании BAHUR. Ты не настоящая пантера и не имитируешь животное (никаких кошачьих метафор).

ХАРАКТЕР: Сдержанная и вежливая хищница. Твой образ: откровенная, сочная, аппетитная, но скромная. Всегда уверена в себе.

ФОРМАТИРОВАНИЕ ОТВЕТОВ:
- Отвечай максимально коротко и по делу
- Пиши в одно-два предложения, без деления на абзацы и переносов строк
- Используй минимум символов и почти не используй смайлы
- Никогда не используй markdown-разметку (**, ##, ``` и т.д.)

ОБРАЩЕНИЕ: Всегда на «Вы». Если клиент переходит границы, отвечай резко, но сохраняя вежливость.

РЕАКЦИЯ НА КОМПЛИМЕНТЫ: Обыкновенно, легко и весело.

ПРИВЕТСТВИЕ: Если клиент пишет повторно, замечай это и вежливо приветствуй. Никогда не прощайся.

АБСТРАКТНЫЕ РАЗГОВОРЫ: Давай минимальные, красивые и талантливые ответы. Не обязательно переводить тему на бизнес.

БИЗНЕС-ЛОГИКА:
- Твоя главная цель — информировать, а не продавать. Предоставляй факты.
- Категорически отказывайся советовать ароматы или делать подборки. Для рекомендаций существует другой агент.
- Не торгуйся. Не обсуждай цены и скидки.
- Если товара нет — сухой факт: «К сожалению, нет в наличии». Не навязывай аналоги.
- Если не знаешь аромат — просто говори: «Не знаю».
- По конкретному аромату выдавай сдержанно: бренд, название.
- Если клиент запросил много ароматов — выдавай только названия, без описаний.

ЛОГИСТИКА:
- У тебя нет офиса, ты только информируешь.
- Отправка товара строго из Грозного. Доставка только по РФ.
- Главное слово бренда: Качество!

ТЕХНИЧЕСКИЕ ПРАВИЛА:
- Помни контекст текущего диалога.
- При системной ошибке — молчи, ничего не отвечай.
- ВСЕ данные о парфюмерии, фабриках, качестве, доставке бери ТОЛЬКО из данных BAHUR ниже. НЕ выдумывай!
- Если информации нет в данных — говори что не знаешь.

ДАННЫЕ КОМПАНИИ BAHUR:
""" + BAHUR_DATA


# --- Telegram API ---
async def telegram_send_message(chat_id, text, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Telegram API error: {resp.status_code} - {resp.text}")
                return False
            return True
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        return False

# --- Supabase ---
async def supabase_get_user(chat_id):
    try:
        url = f"{SUPABASE_URL}/rest/v1/pantera?chat_id=eq.{chat_id}&select=*"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url, headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            })
            if resp.status_code == 200:
                data = resp.json()
                return data[0] if data else None
    except Exception as e:
        logger.error(f"Supabase get error: {e}")
    return None

async def supabase_save_user(chat_id, phone, first_name="", username=""):
    try:
        url = f"{SUPABASE_URL}/rest/v1/pantera"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "phone": phone,
                "first_name": first_name,
                "username": username
            }, headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            })
            if resp.status_code in (200, 201):
                logger.info(f"User saved: chat_id={chat_id}, phone={phone}")
                return True
            logger.error(f"Supabase save error: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Supabase save error: {e}")
    return False

async def request_phone(chat_id):
    await telegram_send_message(
        chat_id,
        "Для начала поделитесь, пожалуйста, Вашим номером телефона.",
        reply_markup={
            "keyboard": [[{"text": "Поделиться номером", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
    )

async def send_typing_action(chat_id):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendChatAction"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            await client.post(url, json={"chat_id": chat_id, "action": "typing"})
    except Exception as e:
        logger.error(f"Typing action error: {e}")

# --- Gemini API ---
async def ask_gemini(question, user_id=None):
    try:
        cfg = load_config()
        model = cfg["model"]
        temperature = cfg["temperature"]
        thinking_budget = cfg["thinking_budget"]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"

        contents = []

        if CONTEXT_ENABLED and user_id:
            try:
                add_user_message(user_id, question)
                user_context = get_user_context(user_id)
                if user_context:
                    for msg in user_context[:-1]:
                        role = "model" if msg["role"] == "assistant" else "user"
                        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
            except Exception as e:
                logger.error(f"Ошибка контекста: {e}")

        contents.append({"role": "user", "parts": [{"text": question}]})

        data = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": 2000,
                "temperature": temperature,
                "thinkingConfig": {
                    "thinkingBudget": thinking_budget
                }
            }
        }

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=data) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Gemini API error: {resp.status} - {error_text}")
                    return None

                result = await resp.json()
                candidates = result.get("candidates", [])
                if not candidates:
                    return None

                parts = candidates[0].get("content", {}).get("parts", [])
                assistant_response = ""
                for part in parts:
                    if "text" in part and "thought" not in part:
                        assistant_response = part["text"].strip()

                if not assistant_response:
                    return None

                assistant_response = assistant_response.replace('*', '').replace('#', '').replace('`', '')

                if CONTEXT_ENABLED and user_id:
                    try:
                        add_assistant_message(user_id, assistant_response)
                    except Exception as e:
                        logger.error(f"Ошибка сохранения контекста: {e}")

                return assistant_response

    except asyncio.TimeoutError:
        logger.error("Gemini API timeout")
        return None
    except Exception as e:
        logger.error(f"Gemini API error: {e}\n{traceback.format_exc()}")
        return None

# --- Обработка голосовых ---
async def process_voice(voice, chat_id, user_id):
    try:
        file_id = voice["file_id"]
        duration = voice.get("duration", 0)
        if duration < 1:
            await telegram_send_message(chat_id, "Голосовое сообщение слишком короткое.")
            return
        if duration > 3600:
            await telegram_send_message(chat_id, "Голосовое сообщение слишком длинное.")
            return
        file_url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(file_url)
            if resp.status_code != 200:
                return
            file_info = resp.json()
            if not file_info.get("ok"):
                return
            file_path = file_info["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
            async with client.stream("GET", download_url) as response:
                if response.status_code != 200:
                    return
                file_content = await response.aread()
                text_content = await recognize_voice_content(file_content)
                if text_content and not any(err in text_content for err in ["Ошибка", "Не удалось", "недоступно"]):
                    ai_answer = await ask_gemini(text_content, user_id)
                    if ai_answer:
                        await telegram_send_message(chat_id, ai_answer)
                else:
                    await telegram_send_message(chat_id, text_content or "Не удалось распознать голосовое сообщение.")
    except Exception as e:
        logger.error(f"Voice processing error: {e}\n{traceback.format_exc()}")

async def recognize_voice_content(file_content):
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        form = aiohttp.FormData()
        form.add_field("file", file_content, filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-1")
        form.add_field("language", "ru")

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data=form
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Whisper API error: {resp.status} - {error_text}")
                    return "Ошибка сервиса распознавания речи."
                result = await resp.json()
                text = result.get("text", "").strip()
                if not text:
                    return "Не удалось разобрать речь."
                return text
    except asyncio.TimeoutError:
        logger.error("Whisper API timeout")
        return "Ошибка сервиса распознавания речи."
    except Exception as e:
        logger.error(f"Whisper API error: {e}")
        return "Ошибка при обработке голосового сообщения."


# --- Обработка фото через GPT-5.2 Vision ---
async def process_photo(photo, message, chat_id, user_id):
    try:
        file_id = photo[-1]["file_id"]
        caption = message.get("caption", "").strip()

        file_url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(file_url)
            if resp.status_code != 200:
                return
            file_info = resp.json()
            if not file_info.get("ok"):
                return
            file_path = file_info["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
            resp = await client.get(download_url)
            if resp.status_code != 200:
                return
            image_data = base64.b64encode(resp.content).decode("utf-8")

        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")

        description = await describe_photo_vision(image_data, mime_type)
        if not description:
            await telegram_send_message(chat_id, "Не удалось распознать фото.")
            return

        if caption:
            prompt = f"Клиент отправил фото с подписью: \"{caption}\"\nОписание фото: {description}\nОтветь клиенту в своём образе."
        else:
            prompt = f"Клиент отправил фото.\nОписание фото: {description}\nОтветь клиенту в своём образе."

        ai_answer = await ask_gemini(prompt, user_id)
        if ai_answer:
            await telegram_send_message(chat_id, ai_answer)
    except Exception as e:
        logger.error(f"Photo processing error: {e}\n{traceback.format_exc()}")


async def describe_photo_vision(image_base64, mime_type="image/jpeg"):
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        data = {
            "model": "gpt-5.2",
            "messages": [
                {
                    "role": "system",
                    "content": "Опиши что на фото кратко на русском."
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 500
        }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=data
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"GPT Vision API error: {resp.status} - {error_text}")
                    return None
                result = await resp.json()
                return result["choices"][0]["message"]["content"].strip()
    except asyncio.TimeoutError:
        logger.error("GPT Vision API timeout")
        return None
    except Exception as e:
        logger.error(f"GPT Vision API error: {e}\n{traceback.format_exc()}")
        return None


# --- Webhook ---
@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: dict, request: Request):
    try:
        logger.info(f"Webhook received update: {list(update.keys())}")
        if "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            user_id = message["from"]["id"]
            text = message.get("text", "").strip()
            voice = message.get("voice")
            photo = message.get("photo")
            contact = message.get("contact")

            # Обработка контакта — сохраняем номер
            if contact:
                phone = contact.get("phone_number", "")
                first_name = contact.get("first_name", "")
                username = message.get("from", {}).get("username", "")
                saved = await supabase_save_user(chat_id, phone, first_name, username)
                if saved:
                    await telegram_send_message(
                        chat_id,
                        "Спасибо! Я Вас запомнила. Задавайте Ваш вопрос.",
                        reply_markup={"remove_keyboard": True}
                    )
                else:
                    await telegram_send_message(chat_id, "Произошла ошибка, попробуйте ещё раз.")
                return {"ok": True}

            # Проверяем регистрацию пользователя
            user = await supabase_get_user(chat_id)

            if text == "/start":
                if user:
                    await telegram_send_message(chat_id, "Здравствуйте, я AI-Пантера, информационный помощник BAHUR. Задавайте Ваш вопрос.")
                else:
                    await request_phone(chat_id)
                return {"ok": True}

            # Если номер не сохранён — просим
            if not user:
                await request_phone(chat_id)
                return {"ok": True}

            if voice:
                await send_typing_action(chat_id)
                await process_voice(voice, chat_id, user_id)
                return {"ok": True}
            if photo:
                await send_typing_action(chat_id)
                await process_photo(photo, message, chat_id, user_id)
                return {"ok": True}
            if text:
                await send_typing_action(chat_id)
                ai_answer = await ask_gemini(text, user_id)
                if ai_answer:
                    await telegram_send_message(chat_id, ai_answer)
                return {"ok": True}
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}\n{traceback.format_exc()}")
        return {"ok": True}


# ===================================================================
# ADMIN PANEL
# ===================================================================
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>PANTERA</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@200;300;400;500;600;700;800;900&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#050505;--glass:rgba(255,255,255,0.04);--glass-border:rgba(255,255,255,0.06);
  --glass-hover:rgba(255,255,255,0.08);--text-primary:rgba(255,255,255,0.92);
  --text-secondary:rgba(255,255,255,0.4);--text-muted:rgba(255,255,255,0.18);--radius:20px;
}
html{font-size:16px;position:fixed;width:100%;height:100%;overflow:hidden}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text-primary);
  width:100%;height:100%;display:flex;align-items:flex-start;justify-content:center;
  padding:12px 20px;overflow-y:auto;overflow-x:hidden;-webkit-font-smoothing:antialiased;
  position:fixed;touch-action:pan-y;overscroll-behavior:none}
body::before{content:'';position:fixed;top:-40%;left:-20%;width:140%;height:140%;
  background:radial-gradient(ellipse at 30% 20%,rgba(255,255,255,0.015) 0%,transparent 60%),
  radial-gradient(ellipse at 70% 80%,rgba(255,255,255,0.01) 0%,transparent 50%);
  pointer-events:none;animation:breathe 12s ease-in-out infinite alternate}
@keyframes breathe{0%{opacity:.6;transform:scale(1)}100%{opacity:1;transform:scale(1.05)}}
.container{width:100%;max-width:440px;position:relative;z-index:1}

/* header */
.header{text-align:center;margin-bottom:20px;padding-top:4px}
.header h1{font-size:2.2rem;font-weight:800;letter-spacing:.25em;text-transform:uppercase;
  color:var(--text-primary);line-height:1;margin-bottom:6px;text-shadow:0 0 80px rgba(255,255,255,.08)}
.header .sub{font-size:.6rem;font-weight:300;letter-spacing:.5em;text-transform:uppercase;color:var(--text-muted)}
.pulse{display:inline-block;width:6px;height:6px;border-radius:50%;background:rgba(120,255,120,.5);
  margin-right:6px;vertical-align:middle;animation:pulse-dot 3s ease-in-out infinite}
@keyframes pulse-dot{0%,100%{opacity:.4;transform:scale(1)}50%{opacity:1;transform:scale(1.3)}}

/* section */
.section{margin-bottom:14px}
.section-label{font-size:.6rem;font-weight:600;letter-spacing:.3em;text-transform:uppercase;
  color:var(--text-muted);margin-bottom:12px;padding-left:4px}
.bento{display:grid;grid-template-columns:1fr 1fr;gap:10px}

/* glass */
.glass{background:var(--glass);backdrop-filter:blur(40px);-webkit-backdrop-filter:blur(40px);
  border:1px solid var(--glass-border);border-radius:var(--radius);
  transition:all .4s cubic-bezier(.16,1,.3,1);position:relative;overflow:hidden}
.glass::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.08),transparent)}
.glass:hover{background:var(--glass-hover);border-color:rgba(255,255,255,.1);
  box-shadow:0 8px 40px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.05);transform:translateY(-1px)}

/* model cards */
.model-card{padding:20px 16px 18px;cursor:pointer;text-align:center}
.model-card .icon-wrap{width:48px;height:48px;margin:0 auto 14px;opacity:.5;transition:all .4s}
.model-card .icon-wrap svg{width:100%;height:100%}
.model-card .name{font-size:1.05rem;font-weight:700;color:var(--text-primary);margin-bottom:4px;letter-spacing:.04em}
.model-card .desc{font-size:.65rem;font-weight:300;color:var(--text-secondary);letter-spacing:.08em}
.model-card .tag{font-size:.55rem;font-weight:400;color:var(--text-muted);margin-top:12px;
  font-family:'SF Mono','Fira Code',monospace;letter-spacing:.05em}
.model-card.active{background:rgba(255,255,255,.07);border-color:rgba(255,255,255,.15);
  box-shadow:0 4px 30px rgba(0,0,0,.3),0 0 60px rgba(255,255,255,.02),inset 0 1px 0 rgba(255,255,255,.1)}
.model-card.active .icon-wrap{opacity:1;transform:scale(1.08)}
.model-card.active .name{color:#fff}
.model-card.active::after{content:'';position:absolute;bottom:0;left:20%;right:20%;height:2px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);border-radius:1px}

/* lock badge */
.lock-badge{position:absolute;top:10px;right:10px;width:18px;height:18px;opacity:.3}
.lock-badge svg{width:100%;height:100%}
.model-card.unlocked .lock-badge{display:none}

/* slider */
.slider-panel{padding:18px;margin-bottom:8px}
.slider-row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px}
.slider-row .label{font-size:.8rem;font-weight:400;color:var(--text-secondary);letter-spacing:.02em}
.slider-row .val{font-size:1.6rem;font-weight:800;color:var(--text-primary);
  font-variant-numeric:tabular-nums;min-width:56px;text-align:right;line-height:1}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:2px;
  background:rgba(255,255,255,.08);border-radius:1px;outline:none;margin:0;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,.05),0 2px 12px rgba(0,0,0,.6);
  cursor:pointer;transition:box-shadow .3s,transform .2s}
input[type=range]::-webkit-slider-thumb:hover{box-shadow:0 0 0 6px rgba(255,255,255,.08),0 2px 20px rgba(0,0,0,.8);transform:scale(1.1)}
input[type=range]::-moz-range-thumb{width:22px;height:22px;border:none;border-radius:50%;background:#fff;
  box-shadow:0 0 0 4px rgba(255,255,255,.05),0 2px 12px rgba(0,0,0,.6);cursor:pointer}
.slider-hints{display:flex;justify-content:space-between;margin-top:10px;font-size:.55rem;
  font-weight:300;color:var(--text-muted);letter-spacing:.1em;text-transform:uppercase}

/* save btn */
.save-btn{width:100%;padding:14px;background:rgba(255,255,255,.06);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.1);border-radius:var(--radius);
  color:var(--text-primary);font-family:inherit;font-size:.75rem;font-weight:600;letter-spacing:.3em;
  text-transform:uppercase;cursor:pointer;transition:all .4s cubic-bezier(.16,1,.3,1);position:relative;
  overflow:hidden;margin-top:8px}
.save-btn::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.12),transparent)}
.save-btn:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.16);
  box-shadow:0 8px 40px rgba(0,0,0,.4);transform:translateY(-1px)}
.save-btn:active{transform:scale(.985);transition:transform .1s}
.save-btn.saved{background:rgba(255,255,255,.12);color:#fff}
.status{text-align:center;margin-top:20px;font-size:.6rem;font-weight:300;color:var(--text-muted);
  letter-spacing:.2em;text-transform:uppercase;min-height:18px;transition:all .4s}
.status.ok{color:rgba(255,255,255,.7)} .status.err{color:rgba(255,80,80,.7)}

/* success overlay */
.success-overlay{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.92);backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);
  opacity:0;pointer-events:none;transition:opacity .4s}
.success-overlay.show{opacity:1;pointer-events:auto}
.success-overlay .check-wrap{text-align:center;animation:scaleIn .5s cubic-bezier(.16,1,.3,1)}
.success-overlay .check-circle{width:80px;height:80px;border-radius:50%;
  border:2px solid rgba(255,255,255,.2);display:flex;align-items:center;justify-content:center;
  margin:0 auto 20px;animation:pulseGlow 1.5s ease-in-out infinite}
.success-overlay .check-circle svg{width:36px;height:36px}
.success-overlay .check-text{font-size:.7rem;font-weight:500;letter-spacing:.4em;text-transform:uppercase;
  color:rgba(255,255,255,.5)}
@keyframes scaleIn{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:scale(1)}}
@keyframes pulseGlow{0%,100%{box-shadow:0 0 20px rgba(255,255,255,.05)}50%{box-shadow:0 0 40px rgba(255,255,255,.15)}}

/* ---- MODAL ---- */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.85);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);z-index:100;display:none;align-items:center;
  justify-content:center;padding:20px;animation:fadeIn .3s}
.overlay.show{display:flex}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:24px;
  padding:36px 28px;max-width:380px;width:100%;text-align:center;position:relative;
  box-shadow:0 24px 80px rgba(0,0,0,.6);animation:slideUp .4s cubic-bezier(.16,1,.3,1)}
@keyframes slideUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}
.modal-close{position:absolute;top:14px;right:14px;width:28px;height:28px;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.08);border-radius:50%;cursor:pointer;display:flex;
  align-items:center;justify-content:center;transition:all .3s}
.modal-close:hover{background:rgba(255,255,255,.12)}
.modal-close svg{width:12px;height:12px}
.modal h2{font-size:1.3rem;font-weight:700;margin-bottom:6px;color:var(--text-primary)}
.modal p{font-size:.75rem;font-weight:300;color:var(--text-secondary);margin-bottom:24px;line-height:1.5}
.modal .step{margin-bottom:20px}
.modal .step-num{font-size:.55rem;font-weight:600;letter-spacing:.3em;color:var(--text-muted);
  text-transform:uppercase;margin-bottom:8px}
.modal .channel-btn{display:block;width:100%;padding:14px;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.08);border-radius:14px;color:var(--text-primary);
  text-decoration:none;font-size:.8rem;font-weight:500;letter-spacing:.05em;transition:all .3s;
  cursor:pointer;font-family:inherit;text-align:center}
.modal .channel-btn:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.14)}
.modal input{width:100%;padding:14px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);border-radius:14px;color:#fff;font-family:inherit;
  font-size:1rem;text-align:center;letter-spacing:.15em;outline:none;transition:border-color .3s}
.modal input::placeholder{color:rgba(255,255,255,.15);letter-spacing:.1em}
.modal input:focus{border-color:rgba(255,255,255,.2)}
.modal .phone-input{font-size:.85rem;letter-spacing:.08em;margin-bottom:10px}
.modal .code-input{font-size:1.4rem;font-weight:700;letter-spacing:.3em}
.modal .unlock-btn{width:100%;padding:16px;background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.1);border-radius:14px;color:var(--text-primary);
  font-family:inherit;font-size:.75rem;font-weight:600;letter-spacing:.2em;text-transform:uppercase;
  cursor:pointer;transition:all .3s;margin-top:16px}
.modal .unlock-btn:hover{background:rgba(255,255,255,.14)}
.modal .unlock-btn:active{transform:scale(.98)}
.modal .error-msg{font-size:.7rem;color:rgba(255,80,80,.7);margin-top:10px;min-height:18px}
.modal .success-msg{font-size:.7rem;color:rgba(120,255,120,.7);margin-top:10px}

@media(max-width:380px){
  .header h1{font-size:2.4rem;letter-spacing:.15em}
  .model-card{padding:20px 14px 18px}
  .slider-panel{padding:18px}
  .modal{padding:28px 20px}
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Pantera</h1>
    <div class="sub"><span class="pulse"></span>управление</div>
  </div>

  <div class="section">
    <div class="section-label">движок</div>
    <div class="bento">
      <div class="glass model-card" data-model="gemini-3-flash-preview" onclick="selectModel(this)">
        <div class="icon-wrap">
          <svg viewBox="0 0 48 48" fill="none" stroke="rgba(255,255,255,0.7)" stroke-width="1.5" stroke-linecap="round">
            <path d="M24 4L28 18H38L30 26L34 40L24 32L14 40L18 26L10 18H20Z"/>
          </svg>
        </div>
        <div class="name">Молния</div>
        <div class="desc">быстрая и точная</div>
        <div class="tag">3-flash</div>
      </div>
      <div class="glass model-card" data-model="gemini-3.1-pro-preview" id="proCard" onclick="handleProClick(this)">
        <div class="lock-badge" id="lockBadge">
          <svg viewBox="0 0 18 18" fill="none" stroke="rgba(255,255,255,0.5)" stroke-width="1.2" stroke-linecap="round">
            <rect x="3" y="8" width="12" height="8" rx="2"/><path d="M6 8V5a3 3 0 0 1 6 0v3"/>
          </svg>
        </div>
        <div class="icon-wrap">
          <svg viewBox="0 0 48 48" fill="none" stroke="rgba(255,255,255,0.7)" stroke-width="1.5" stroke-linecap="round">
            <path d="M24 4L6 14V34L24 44L42 34V14Z"/><path d="M24 4V44"/><path d="M6 14L42 34"/><path d="M42 14L6 34"/>
          </svg>
        </div>
        <div class="name">Хищница</div>
        <div class="desc">мощная и глубокая</div>
        <div class="tag">3.1-pro</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-label">параметры</div>
    <div class="glass slider-panel">
      <div class="slider-row">
        <span class="label">Температура</span>
        <span class="val" id="tempVal">0.7</span>
      </div>
      <input type="range" id="tempSlider" min="0" max="2" step="0.1" value="0.7"
             oninput="document.getElementById('tempVal').textContent=parseFloat(this.value).toFixed(1)">
      <div class="slider-hints"><span>точная</span><span>креативная</span></div>
    </div>
    <div class="glass slider-panel">
      <div class="slider-row">
        <span class="label">Глубина мышления</span>
        <span class="val" id="thinkVal">1024</span>
      </div>
      <input type="range" id="thinkSlider" min="0" max="8192" step="128" value="1024"
             oninput="document.getElementById('thinkVal').textContent=this.value">
      <div class="slider-hints"><span>мгновенно</span><span>глубоко</span></div>
    </div>
  </div>

  <button class="save-btn" id="saveBtn" onclick="saveConfig()">применить</button>
  <div class="status" id="status"></div>
</div>

<!-- PRO UNLOCK MODAL -->
<div class="overlay" id="proModal">
  <div class="modal">
    <div class="modal-close" onclick="closeModal()">
      <svg viewBox="0 0 12 12" fill="none" stroke="rgba(255,255,255,0.5)" stroke-width="1.5" stroke-linecap="round">
        <path d="M1 1L11 11M11 1L1 11"/>
      </svg>
    </div>
    <h2>Хищница</h2>
    <p>Pro-версия доступна подписчикам канала</p>

    <div class="step">
      <div class="step-num">шаг 1 — подписка</div>
      <a href="https://t.me/+tHEoJ0Wt27o5YzEy" target="_blank" class="channel-btn">
        <svg style="width:14px;height:14px;vertical-align:-2px;margin-right:6px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9Z"/></svg>
        Подписаться на канал
      </a>
    </div>

    <div class="step">
      <div class="step-num">шаг 2 — номер телефона</div>
      <div id="phoneShared" style="display:none;padding:14px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:14px;font-size:.85rem;color:rgba(255,255,255,.7)">
        <svg style="width:14px;height:14px;vertical-align:-2px;margin-right:4px" viewBox="0 0 16 16" fill="none" stroke="rgba(120,255,120,.6)" stroke-width="1.5" stroke-linecap="round"><path d="M2 8.5L6 12.5L14 3.5"/></svg>
        <span id="phoneDisplay"></span>
      </div>
      <button class="channel-btn" id="sharePhoneBtn" onclick="requestPhone()">
        <svg style="width:14px;height:14px;vertical-align:-2px;margin-right:6px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6A19.79 19.79 0 012.12 4.18 2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>
        Поделиться номером
      </button>
      <input type="tel" class="phone-input" id="phoneInputFallback" placeholder="+7 (___) ___-__-__" maxlength="18" style="display:none;margin-top:8px">
    </div>

    <div class="step">
      <div class="step-num">шаг 3 — код доступа</div>
      <input type="text" class="code-input" id="codeInput" placeholder="______" maxlength="7" inputmode="numeric">
    </div>

    <button class="unlock-btn" onclick="unlockPro()">Разблокировать</button>
    <div class="error-msg" id="modalError"></div>
  </div>
</div>

<!-- SUCCESS OVERLAY -->
<div class="success-overlay" id="successOverlay">
  <div class="check-wrap">
    <div class="check-circle">
      <svg viewBox="0 0 36 36" fill="none" stroke="rgba(255,255,255,0.8)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M8 18L15 25L28 11"/>
      </svg>
    </div>
    <div class="check-text">применено</div>
  </div>
</div>

<script>
let selectedModel='gemini-3-flash-preview';
let proUnlocked=localStorage.getItem('pantera_pro')==='1';
let userPhone='';
const tg=window.Telegram&&window.Telegram.WebApp;
const isTgWebApp=tg&&tg.initData&&tg.initData.length>0;

// init telegram webapp
if(isTgWebApp){
  tg.ready();
  tg.expand();
  tg.setHeaderColor('#050505');
  tg.setBackgroundColor('#050505');
}

function initUI(){
  if(proUnlocked) document.getElementById('proCard').classList.add('unlocked');
  // if not in Telegram, show fallback phone input
  if(!isTgWebApp){
    document.getElementById('sharePhoneBtn').style.display='none';
    document.getElementById('phoneInputFallback').style.display='block';
  }
}
initUI();

function selectModel(el){
  document.querySelectorAll('.model-card').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  selectedModel=el.dataset.model;
}

function handleProClick(el){
  if(proUnlocked){selectModel(el)}
  else{document.getElementById('proModal').classList.add('show')}
}

function closeModal(){
  document.getElementById('proModal').classList.remove('show');
  document.getElementById('modalError').textContent='';
}

// Telegram native phone request
function requestPhone(){
  if(isTgWebApp&&tg.requestContact){
    tg.requestContact(function(ok,evt){
      if(ok&&evt&&evt.responseUnsafe&&evt.responseUnsafe.contact){
        const contact=evt.responseUnsafe.contact;
        userPhone=contact.phone_number||'';
        document.getElementById('sharePhoneBtn').style.display='none';
        document.getElementById('phoneShared').style.display='block';
        document.getElementById('phoneDisplay').textContent=formatPhone(userPhone);
      }
    });
  }else{
    // fallback: show manual input
    document.getElementById('sharePhoneBtn').style.display='none';
    document.getElementById('phoneInputFallback').style.display='block';
  }
}

function formatPhone(p){
  let d=p.replace(/\D/g,'');
  if(d.startsWith('8'))d='7'+d.slice(1);
  if(!d.startsWith('7')&&d.length===10)d='7'+d;
  if(d.length>=11)return '+'+d[0]+' ('+d.slice(1,4)+') '+d.slice(4,7)+'-'+d.slice(7,9)+'-'+d.slice(9,11);
  return '+'+d;
}

// fallback phone mask
document.getElementById('phoneInputFallback').addEventListener('input',function(e){
  let v=e.target.value.replace(/\D/g,'');
  if(v.startsWith('8'))v='7'+v.slice(1);
  if(!v.startsWith('7'))v='7'+v;
  let f='+7';
  if(v.length>1)f+=' ('+v.slice(1,4);
  if(v.length>4)f+=') '+v.slice(4,7);
  if(v.length>7)f+='-'+v.slice(7,9);
  if(v.length>9)f+='-'+v.slice(9,11);
  e.target.value=f;
});

// code mask
document.getElementById('codeInput').addEventListener('input',function(e){
  let v=e.target.value.replace(/\D/g,'').slice(0,6);
  if(v.length>3)v=v.slice(0,3)+' '+v.slice(3);
  e.target.value=v;
});

async function unlockPro(){
  const fallbackInput=document.getElementById('phoneInputFallback');
  const phone=userPhone||fallbackInput.value.replace(/\D/g,'');
  const code=document.getElementById('codeInput').value.replace(/\D/g,'');
  const err=document.getElementById('modalError');

  if(phone.length<10){err.textContent='Поделитесь номером телефона';return}
  if(code.length<6){err.textContent='Введите 6-значный код';return}

  try{
    const resp=await fetch('/pantera/unlock',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phone:phone,code:code})
    });
    const data=await resp.json();
    if(data.ok){
      proUnlocked=true;
      localStorage.setItem('pantera_pro','1');
      document.getElementById('proCard').classList.add('unlocked');
      closeModal();
      selectModel(document.getElementById('proCard'));
      if(isTgWebApp)tg.showAlert('Хищница разблокирована');
    }else{
      err.textContent=data.error||'Неверный код';
    }
  }catch(e){
    err.textContent='Ошибка соединения';
  }
}

document.getElementById('proModal').addEventListener('click',function(e){
  if(e.target===this)closeModal();
});

async function saveConfig(){
  const btn=document.getElementById('saveBtn');
  const status=document.getElementById('status');
  btn.textContent='...';
  try{
    const resp=await fetch('/pantera/save',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        model:selectedModel,
        temperature:parseFloat(document.getElementById('tempSlider').value),
        thinking_budget:parseInt(document.getElementById('thinkSlider').value)
      })
    });
    const data=await resp.json();
    if(data.ok){
      // show success overlay
      document.getElementById('successOverlay').classList.add('show');
      // haptic feedback in Telegram
      if(isTgWebApp&&tg.HapticFeedback)tg.HapticFeedback.notificationOccurred('success');
      // close app after delay
      setTimeout(()=>{
        if(isTgWebApp){tg.close()}
        else{
          document.getElementById('successOverlay').classList.remove('show');
          btn.textContent='применить';
          status.textContent='настройки сохранены';status.className='status ok';
        }
      },1200);
    }else{throw new Error('fail')}
  }catch(e){
    btn.textContent='ошибка';status.textContent='не удалось сохранить';status.className='status err';
    setTimeout(()=>{btn.textContent='применить'},2500);
  }
}

fetch('/pantera/config').then(r=>r.json()).then(cfg=>{
  selectedModel=cfg.model||'gemini-3-flash-preview';
  document.querySelectorAll('.model-card').forEach(c=>{
    if(c.dataset.model===selectedModel)c.classList.add('active');
  });
  if(selectedModel==='gemini-3.1-pro-preview'&&!proUnlocked){
    selectedModel='gemini-3-flash-preview';
    document.querySelector('[data-model="gemini-3-flash-preview"]').classList.add('active');
  }
  document.getElementById('tempSlider').value=cfg.temperature||0.7;
  document.getElementById('tempVal').textContent=parseFloat(cfg.temperature||0.7).toFixed(1);
  document.getElementById('thinkSlider').value=cfg.thinking_budget||1024;
  document.getElementById('thinkVal').textContent=cfg.thinking_budget||1024;
});
</script>
</body>
</html>"""

ACCESS_CODE = os.getenv("PRO_ACCESS_CODE", "888888")

@app.get("/pantera", response_class=HTMLResponse)
async def admin_panel():
    return ADMIN_HTML

@app.get("/pantera/config")
async def get_config():
    return JSONResponse(load_config())

@app.post("/pantera/save")
async def save_config_endpoint(request: Request):
    try:
        body = await request.json()
        cfg = {
            "model": body.get("model", DEFAULT_CONFIG["model"]),
            "temperature": float(body.get("temperature", DEFAULT_CONFIG["temperature"])),
            "thinking_budget": int(body.get("thinking_budget", DEFAULT_CONFIG["thinking_budget"]))
        }
        if save_config(cfg):
            logger.info(f"Config updated: {cfg}")
            return JSONResponse({"ok": True, "config": cfg})
        return JSONResponse({"ok": False}, status_code=500)
    except Exception as e:
        logger.error(f"Save config error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/pantera/unlock")
async def unlock_pro(request: Request):
    try:
        body = await request.json()
        phone = body.get("phone", "").strip()
        code = body.get("code", "").strip()
        if code == ACCESS_CODE:
            logger.info(f"Pro unlocked by phone: {phone}")
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "Неверный код"})
    except Exception as e:
        logger.error(f"Unlock error: {e}")
        return JSONResponse({"ok": False, "error": "Ошибка сервера"})


# --- Webhook setup ---
async def set_telegram_webhook(base_url: str):
    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            data={"url": webhook_url}
        )
        logger.info(f"Set webhook: {resp.text}")
        return resp.json()

@app.on_event("startup")
async def startup_event():
    logger.info("=== STARTUP ===")
    base_url = os.getenv("WEBHOOK_BASE_URL")
    if base_url:
        try:
            result = await set_telegram_webhook(base_url)
            logger.info(f"Webhook result: {result}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.error("WEBHOOK_BASE_URL not set! Bot will NOT receive messages!")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("=== SHUTDOWN ===")

@app.get("/")
async def healthcheck():
    return PlainTextResponse("OK")

# --- Запуск ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("1:app", host="0.0.0.0", port=port)
