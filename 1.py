import logging
import os
import traceback
import random
import aiohttp
import asyncio
import httpx
import sys
import uvicorn
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel
from contextlib import contextmanager

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TOKEN = os.getenv('TOKEN')
BASE_WEBHOOK_URL = os.getenv('WEBHOOK_BASE_URL')
WEBHOOK_PATH = "/webhook/ai-bear-123456"
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.1-pro-preview')

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
async def telegram_send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
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
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

        # Собираем историю диалога в формате Gemini
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
                "temperature": 0.7,
                "thinkingConfig": {
                    "thinkingBudget": 1024
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
                    logger.error(f"Gemini: no candidates in response")
                    return None

                parts = candidates[0].get("content", {}).get("parts", [])
                # Берём только текстовые части (пропускаем thinking)
                assistant_response = ""
                for part in parts:
                    if "text" in part and "thought" not in part:
                        assistant_response = part["text"].strip()

                if not assistant_response:
                    logger.error(f"Gemini: no text in response parts")
                    return None

                # Убираем markdown
                assistant_response = assistant_response.replace('*', '').replace('#', '').replace('`', '')

                # Сохраняем ответ в контекст
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

# --- Обработка голосовых сообщений ---
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

        # Получаем файл
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
        import speech_recognition as sr
        from pydub import AudioSegment
        import tempfile
        recognizer = sr.Recognizer()
        with tempfile.NamedTemporaryFile(suffix='.ogg') as temp_ogg, tempfile.NamedTemporaryFile(suffix='.wav') as temp_wav:
            temp_ogg.write(file_content)
            temp_ogg.flush()
            try:
                audio = AudioSegment.from_file(temp_ogg.name)
                audio.export(temp_wav.name, format='wav')
            except Exception:
                return "Ошибка при обработке аудио файла."
            try:
                with sr.AudioFile(temp_wav.name) as source:
                    audio_data = recognizer.record(source)
                return recognizer.recognize_google(audio_data, language='ru-RU')
            except sr.UnknownValueError:
                return "Не удалось разобрать речь."
            except sr.RequestError:
                return "Ошибка сервиса распознавания речи."
    except Exception as e:
        logger.error(f"Speech recognition error: {e}")
        return "Ошибка при обработке голосового сообщения."


# --- Webhook ---
@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: dict, request: Request):
    try:
        # --- Обычное сообщение ---
        if "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            user_id = message["from"]["id"]
            text = message.get("text", "").strip()
            voice = message.get("voice")

            # Голосовое сообщение
            if voice:
                await send_typing_action(chat_id)
                await process_voice(voice, chat_id, user_id)
                return {"ok": True}

            # /start
            if text == "/start":
                welcome = "Здравствуйте, я AI-Пантера, информационный помощник BAHUR. Задавайте Ваш вопрос."
                await telegram_send_message(chat_id, welcome)
                return {"ok": True}

            # Любое текстовое сообщение -> AI
            if text:
                await send_typing_action(chat_id)
                ai_answer = await ask_gemini(text, user_id)
                if ai_answer:
                    await telegram_send_message(chat_id, ai_answer)
                # При ошибке — молчим (по правилам персонажа)
                return {"ok": True}

        return {"ok": True}

    except Exception as e:
        logger.error(f"Webhook error: {e}\n{traceback.format_exc()}")
        # При ошибке — молчим
        return {"ok": True}


# --- Установка webhook при старте ---
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
            await set_telegram_webhook(base_url)
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")

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
