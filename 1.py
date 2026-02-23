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
OPENAI_API = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5')
OPENAI_FALLBACK_MODEL = os.getenv('OPENAI_FALLBACK_MODEL', 'gpt-4o-mini')

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

# --- OpenAI API ---
async def ask_chatgpt(question, user_id=None):
    try:
        model_lower = (OPENAI_MODEL or "").lower()
        use_responses_api = model_lower.startswith("gpt-5") or model_lower.startswith("gpt-4.1") or model_lower.startswith("gpt-4o")

        url = "https://api.openai.com/v1/responses" if use_responses_api else "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API}",
            "Content-Type": "application/json"
        }

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Добавляем контекст разговора
        if CONTEXT_ENABLED and user_id:
            try:
                add_user_message(user_id, question)
                user_context = get_user_context(user_id)
                if user_context:
                    context_messages = user_context[:-1]
                    messages.extend(context_messages)
                messages.append({"role": "user", "content": question})
            except Exception as e:
                logger.error(f"Ошибка контекста: {e}")
                messages.append({"role": "user", "content": question})
        else:
            messages.append({"role": "user", "content": question})

        if use_responses_api:
            responses_input = []
            system_instructions = None
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    system_instructions = f"{system_instructions}\n\n{content}" if system_instructions else content
                    continue
                content_type = "output_text" if role == "assistant" else "input_text"
                responses_input.append({
                    "role": role,
                    "content": [{"type": content_type, "text": content}]
                })
            data = {
                "model": OPENAI_MODEL,
                "input": responses_input,
                "max_output_tokens": 8192,
                "reasoning": {"effort": "low"}
            }
            if system_instructions:
                data["instructions"] = system_instructions
        else:
            data = {
                "model": OPENAI_MODEL,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 2000
            }

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=data) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    # Fallback на chat/completions
                    if use_responses_api and resp.status == 401 and "api.responses.write" in (error_text or ""):
                        fb_url = "https://api.openai.com/v1/chat/completions"
                        fb_data = {
                            "model": OPENAI_FALLBACK_MODEL,
                            "messages": messages,
                            "temperature": 0.7,
                            "max_tokens": 1000
                        }
                        async with session.post(fb_url, headers=headers, json=fb_data) as fb_resp:
                            if fb_resp.status != 200:
                                logger.error(f"OpenAI fallback error: {fb_resp.status}")
                                return None
                            fb_result = await fb_resp.json()
                            if "choices" not in fb_result or not fb_result["choices"]:
                                return None
                            assistant_response = fb_result["choices"][0]["message"]["content"].strip()
                    else:
                        logger.error(f"OpenAI API error: {resp.status} - {error_text}")
                        return None
                else:
                    result = await resp.json()
                    if use_responses_api:
                        assistant_response = None
                        if isinstance(result, dict):
                            assistant_response = (result.get("output_text") or "").strip()
                            if not assistant_response:
                                output = result.get("output") or []
                                for item in (output if isinstance(output, list) else []):
                                    if isinstance(item, dict):
                                        for c in (item.get("content") or []):
                                            if isinstance(c, dict):
                                                text_val = c.get("text") or c.get("output_text")
                                                if text_val:
                                                    assistant_response = str(text_val).strip()
                                                    break
                                        if assistant_response:
                                            break
                        if not assistant_response:
                            return None
                    else:
                        if "choices" not in result or not result["choices"]:
                            return None
                        assistant_response = result["choices"][0]["message"]["content"].strip()

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
        logger.error("OpenAI API timeout")
        return None
    except Exception as e:
        logger.error(f"OpenAI API error: {e}\n{traceback.format_exc()}")
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
                    ai_answer = await ask_chatgpt(text_content, user_id)
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
                ai_answer = await ask_chatgpt(text, user_id)
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
