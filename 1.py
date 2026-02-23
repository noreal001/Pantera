import logging
import os
import traceback
import aiohttp
import asyncio
import httpx
import sys
import uvicorn
import json
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
        if "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            user_id = message["from"]["id"]
            text = message.get("text", "").strip()
            voice = message.get("voice")
            if voice:
                await send_typing_action(chat_id)
                await process_voice(voice, chat_id, user_id)
                return {"ok": True}
            if text == "/start":
                welcome = "Здравствуйте, я AI-Пантера, информационный помощник BAHUR. Задавайте Ваш вопрос."
                await telegram_send_message(chat_id, welcome)
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
ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>PANTERA</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@200;300;400;500;600;700;800;900&display=swap');

*{margin:0;padding:0;box-sizing:border-box}

:root{
  --bg:#050505;
  --surface:rgba(255,255,255,0.03);
  --glass:rgba(255,255,255,0.04);
  --glass-border:rgba(255,255,255,0.06);
  --glass-hover:rgba(255,255,255,0.08);
  --text-primary:rgba(255,255,255,0.92);
  --text-secondary:rgba(255,255,255,0.4);
  --text-muted:rgba(255,255,255,0.18);
  --accent:rgba(255,255,255,0.9);
  --glow:rgba(255,255,255,0.06);
  --success:rgba(255,255,255,0.7);
  --radius:20px;
}

html{font-size:16px}

body{
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  background:var(--bg);
  color:var(--text-primary);
  min-height:100vh;
  min-height:100dvh;
  display:flex;
  align-items:center;
  justify-content:center;
  padding:20px;
  overflow-x:hidden;
  -webkit-font-smoothing:antialiased;
}

/* ambient glow */
body::before{
  content:'';
  position:fixed;
  top:-40%;left:-20%;
  width:140%;height:140%;
  background:radial-gradient(ellipse at 30% 20%,rgba(255,255,255,0.015) 0%,transparent 60%),
             radial-gradient(ellipse at 70% 80%,rgba(255,255,255,0.01) 0%,transparent 50%);
  pointer-events:none;
  animation:breathe 12s ease-in-out infinite alternate;
}
@keyframes breathe{
  0%{opacity:0.6;transform:scale(1)}
  100%{opacity:1;transform:scale(1.05)}
}

.container{
  width:100%;
  max-width:440px;
  position:relative;
  z-index:1;
}

/* ---- HEADER ---- */
.header{
  text-align:center;
  margin-bottom:48px;
  padding-top:12px;
}
.header h1{
  font-size:3.2rem;
  font-weight:800;
  letter-spacing:0.25em;
  text-transform:uppercase;
  color:var(--text-primary);
  line-height:1;
  margin-bottom:8px;
  text-shadow:0 0 80px rgba(255,255,255,0.08);
}
.header .sub{
  font-size:0.65rem;
  font-weight:300;
  letter-spacing:0.5em;
  text-transform:uppercase;
  color:var(--text-muted);
}

/* ---- SECTION ---- */
.section{margin-bottom:20px}
.section-label{
  font-size:0.6rem;
  font-weight:600;
  letter-spacing:0.3em;
  text-transform:uppercase;
  color:var(--text-muted);
  margin-bottom:12px;
  padding-left:4px;
}

/* ---- BENTO GRID ---- */
.bento{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:10px;
}

/* ---- GLASS CARD ---- */
.glass{
  background:var(--glass);
  backdrop-filter:blur(40px);
  -webkit-backdrop-filter:blur(40px);
  border:1px solid var(--glass-border);
  border-radius:var(--radius);
  transition:all 0.4s cubic-bezier(0.16,1,0.3,1);
  position:relative;
  overflow:hidden;
}
.glass::before{
  content:'';
  position:absolute;
  top:0;left:0;right:0;
  height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.08),transparent);
}
.glass:hover{
  background:var(--glass-hover);
  border-color:rgba(255,255,255,0.1);
  box-shadow:0 8px 40px rgba(0,0,0,0.4),inset 0 1px 0 rgba(255,255,255,0.05);
  transform:translateY(-1px);
}

/* ---- MODEL CARDS ---- */
.model-card{
  padding:28px 20px 24px;
  cursor:pointer;
  text-align:center;
}
.model-card .icon{
  font-size:2.4rem;
  display:block;
  margin-bottom:14px;
  filter:grayscale(1) brightness(0.8);
  transition:all 0.4s;
}
.model-card .name{
  font-size:1.05rem;
  font-weight:700;
  color:var(--text-primary);
  margin-bottom:4px;
  letter-spacing:0.04em;
}
.model-card .desc{
  font-size:0.65rem;
  font-weight:300;
  color:var(--text-secondary);
  letter-spacing:0.08em;
}
.model-card .tag{
  font-size:0.55rem;
  font-weight:400;
  color:var(--text-muted);
  margin-top:12px;
  font-family:'SF Mono','Fira Code',monospace;
  letter-spacing:0.05em;
}
.model-card.active{
  background:rgba(255,255,255,0.07);
  border-color:rgba(255,255,255,0.15);
  box-shadow:0 4px 30px rgba(0,0,0,0.3),0 0 60px rgba(255,255,255,0.02),inset 0 1px 0 rgba(255,255,255,0.1);
}
.model-card.active .icon{
  filter:grayscale(0) brightness(1);
  transform:scale(1.08);
}
.model-card.active .name{color:#fff}
.model-card.active::after{
  content:'';
  position:absolute;
  bottom:0;left:20%;right:20%;
  height:2px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.3),transparent);
  border-radius:1px;
}

/* ---- SLIDER PANEL ---- */
.slider-panel{
  padding:24px;
  margin-bottom:10px;
}
.slider-row{
  display:flex;
  justify-content:space-between;
  align-items:baseline;
  margin-bottom:16px;
}
.slider-row .label{
  font-size:0.8rem;
  font-weight:400;
  color:var(--text-secondary);
  letter-spacing:0.02em;
}
.slider-row .val{
  font-size:1.6rem;
  font-weight:800;
  color:var(--text-primary);
  font-variant-numeric:tabular-nums;
  min-width:56px;
  text-align:right;
  line-height:1;
}

/* range */
input[type=range]{
  -webkit-appearance:none;
  appearance:none;
  width:100%;
  height:2px;
  background:rgba(255,255,255,0.08);
  border-radius:1px;
  outline:none;
  margin:0;
  cursor:pointer;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;
  width:22px;height:22px;
  border-radius:50%;
  background:#fff;
  box-shadow:0 0 0 4px rgba(255,255,255,0.05),0 2px 12px rgba(0,0,0,0.6);
  cursor:pointer;
  transition:box-shadow 0.3s,transform 0.2s;
}
input[type=range]::-webkit-slider-thumb:hover{
  box-shadow:0 0 0 6px rgba(255,255,255,0.08),0 2px 20px rgba(0,0,0,0.8);
  transform:scale(1.1);
}
input[type=range]:active::-webkit-slider-thumb{
  transform:scale(0.95);
  box-shadow:0 0 0 8px rgba(255,255,255,0.1),0 2px 8px rgba(0,0,0,0.6);
}
/* firefox */
input[type=range]::-moz-range-thumb{
  width:22px;height:22px;border:none;
  border-radius:50%;background:#fff;
  box-shadow:0 0 0 4px rgba(255,255,255,0.05),0 2px 12px rgba(0,0,0,0.6);
  cursor:pointer;
}

.slider-hints{
  display:flex;
  justify-content:space-between;
  margin-top:10px;
  font-size:0.55rem;
  font-weight:300;
  color:var(--text-muted);
  letter-spacing:0.1em;
  text-transform:uppercase;
}

/* ---- SAVE BTN ---- */
.save-btn{
  width:100%;
  padding:18px;
  background:rgba(255,255,255,0.06);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border:1px solid rgba(255,255,255,0.1);
  border-radius:var(--radius);
  color:var(--text-primary);
  font-family:inherit;
  font-size:0.75rem;
  font-weight:600;
  letter-spacing:0.3em;
  text-transform:uppercase;
  cursor:pointer;
  transition:all 0.4s cubic-bezier(0.16,1,0.3,1);
  position:relative;
  overflow:hidden;
  margin-top:8px;
}
.save-btn::before{
  content:'';
  position:absolute;
  top:0;left:0;right:0;
  height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.12),transparent);
}
.save-btn:hover{
  background:rgba(255,255,255,0.1);
  border-color:rgba(255,255,255,0.16);
  box-shadow:0 8px 40px rgba(0,0,0,0.4),0 0 60px rgba(255,255,255,0.02);
  transform:translateY(-1px);
}
.save-btn:active{transform:scale(0.985);transition:transform 0.1s}
.save-btn.saved{
  background:rgba(255,255,255,0.12);
  color:#fff;
  box-shadow:0 0 40px rgba(255,255,255,0.04);
}

/* ---- STATUS ---- */
.status{
  text-align:center;
  margin-top:20px;
  font-size:0.6rem;
  font-weight:300;
  color:var(--text-muted);
  letter-spacing:0.2em;
  text-transform:uppercase;
  min-height:18px;
  transition:all 0.4s;
}
.status.ok{color:var(--success)}
.status.err{color:rgba(255,80,80,0.7)}

/* ---- PULSE DOT ---- */
.pulse{
  display:inline-block;
  width:6px;height:6px;
  border-radius:50%;
  background:rgba(255,255,255,0.3);
  margin-right:6px;
  vertical-align:middle;
  animation:pulse-dot 3s ease-in-out infinite;
}
.pulse.live{background:rgba(120,255,120,0.5)}
@keyframes pulse-dot{
  0%,100%{opacity:0.4;transform:scale(1)}
  50%{opacity:1;transform:scale(1.3)}
}

/* ---- RESPONSIVE ---- */
@media(max-width:380px){
  .header h1{font-size:2.4rem;letter-spacing:0.15em}
  .model-card{padding:20px 14px 18px}
  .model-card .icon{font-size:2rem}
  .slider-panel{padding:18px}
}
</style>
</head>
<body>

<div class="container">

  <div class="header">
    <h1>Pantera</h1>
    <div class="sub"><span class="pulse live"></span>control</div>
  </div>

  <div class="section">
    <div class="section-label">engine</div>
    <div class="bento">
      <div class="glass model-card" data-model="gemini-3-flash-preview" onclick="selectModel(this)">
        <span class="icon">&#9889;</span>
        <div class="name">Flash</div>
        <div class="desc">fast &middot; precise</div>
        <div class="tag">3-flash</div>
      </div>
      <div class="glass model-card" data-model="gemini-3.1-pro-preview" onclick="selectModel(this)">
        <span class="icon">&#9670;</span>
        <div class="name">Pro</div>
        <div class="desc">deep &middot; powerful</div>
        <div class="tag">3.1-pro</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-label">parameters</div>

    <div class="glass slider-panel">
      <div class="slider-row">
        <span class="label">Temperature</span>
        <span class="val" id="tempVal">0.7</span>
      </div>
      <input type="range" id="tempSlider" min="0" max="2" step="0.1" value="0.7"
             oninput="document.getElementById('tempVal').textContent=parseFloat(this.value).toFixed(1)">
      <div class="slider-hints">
        <span>precise</span>
        <span>creative</span>
      </div>
    </div>

    <div class="glass slider-panel">
      <div class="slider-row">
        <span class="label">Thinking depth</span>
        <span class="val" id="thinkVal">1024</span>
      </div>
      <input type="range" id="thinkSlider" min="0" max="8192" step="128" value="1024"
             oninput="document.getElementById('thinkVal').textContent=this.value">
      <div class="slider-hints">
        <span>instant</span>
        <span>deep</span>
      </div>
    </div>
  </div>

  <button class="save-btn" id="saveBtn" onclick="saveConfig()">apply</button>
  <div class="status" id="status"></div>

</div>

<script>
let selectedModel='gemini-3-flash-preview';

function selectModel(el){
  document.querySelectorAll('.model-card').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  selectedModel=el.dataset.model;
}

async function saveConfig(){
  const btn=document.getElementById('saveBtn');
  const status=document.getElementById('status');
  btn.textContent='...';
  try{
    const resp=await fetch('/pantera/save',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        model:selectedModel,
        temperature:parseFloat(document.getElementById('tempSlider').value),
        thinking_budget:parseInt(document.getElementById('thinkSlider').value)
      })
    });
    const data=await resp.json();
    if(data.ok){
      btn.textContent='applied';
      btn.classList.add('saved');
      status.textContent='settings saved';
      status.className='status ok';
      setTimeout(()=>{btn.textContent='apply';btn.classList.remove('saved')},2500);
    }else{throw new Error('fail')}
  }catch(e){
    btn.textContent='error';
    status.textContent='failed to save';
    status.className='status err';
    setTimeout(()=>{btn.textContent='apply'},2500);
  }
}

fetch('/pantera/config').then(r=>r.json()).then(cfg=>{
  selectedModel=cfg.model||'gemini-3-flash-preview';
  document.querySelectorAll('.model-card').forEach(c=>{
    if(c.dataset.model===selectedModel) c.classList.add('active');
  });
  document.getElementById('tempSlider').value=cfg.temperature||0.7;
  document.getElementById('tempVal').textContent=parseFloat(cfg.temperature||0.7).toFixed(1);
  document.getElementById('thinkSlider').value=cfg.thinking_budget||1024;
  document.getElementById('thinkVal').textContent=cfg.thinking_budget||1024;
});
</script>
</body>
</html>"""

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
