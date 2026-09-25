import asyncio
import os
import random
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command, Filter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ChatAction, ParseMode
from html import escape as hescape
from phrases import PHRASES
from agent import chat_with_agent, init_chat_history

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
if not BOT_TOKEN or not ALLOWED_USER_ID:
    raise ValueError("BOT_TOKEN и ALLOWED_USER_ID должны быть в .env")

DB_PATH = "user_states.db"
_active_tasks: dict[int, asyncio.Task] = {}


# --- Access Filter ---
class IsOwner(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return message.from_user.id == ALLOWED_USER_ID


# --- DB ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS used_phrases (
                    user_id INTEGER, phrase_idx INTEGER,
                    PRIMARY KEY (user_id, phrase_idx))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_states (
                    user_id INTEGER PRIMARY KEY,
                    thinking INTEGER DEFAULT 1,
                    terminal INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()


def get_used(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT phrase_idx FROM used_phrases WHERE user_id=?", (user_id,))
    used = {r[0] for r in c.fetchall()}
    conn.close()
    return used


def mark_used(user_id, idx):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO used_phrases VALUES (?,?)", (user_id, idx))
    conn.commit()
    conn.close()


def reset_used(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM used_phrases WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_state(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT thinking, terminal FROM user_states WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"thinking": bool(row[0]), "terminal": bool(row[1])}
    return {"thinking": True, "terminal": False}


def set_state(user_id, thinking=None, terminal=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    current = get_state(user_id)
    new_thinking = thinking if thinking is not None else current["thinking"]
    new_terminal = terminal if terminal is not None else current["terminal"]
    c.execute(
        "INSERT INTO user_states (user_id, thinking, terminal) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET thinking=?, terminal=?",
        (user_id, int(new_thinking), int(new_terminal), int(new_thinking), int(new_terminal))
    )
    conn.commit()
    conn.close()
    return {"thinking": new_thinking, "terminal": new_terminal}


# --- Markdown → HTML Converter ---
def markdown_to_html(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def replace_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2).strip()
        if lang:
            return f'<pre><code class="language-{lang}">{code}</code></pre>'
        return f'<pre><code>{code}</code></pre>'
    text = re.sub(r'```(\w*)\n?(.*?)```', replace_code_block, text, flags=re.DOTALL)

    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*(?!\*)(.+?)(?<!\*)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\|\|(.+?)\|\|', r'<tg-spoiler>\1</tg-spoiler>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    lines = text.split('\n')
    result_lines = []
    in_ul = False
    in_ol = False

    for line in lines:
        ul_match = re.match(r'^[\-\*]\s+(.*)', line)
        ol_match = re.match(r'^\d+\.\s+(.*)', line)

        if ul_match:
            if not in_ul:
                if in_ol:
                    result_lines.append('</ol>')
                    in_ol = False
                result_lines.append('<ul>')
                in_ul = True
            result_lines.append(f'<li>{ul_match.group(1)}</li>')
        elif ol_match:
            if not in_ol:
                if in_ul:
                    result_lines.append('</ul>')
                    in_ul = False
                result_lines.append('<ol>')
                in_ol = True
            result_lines.append(f'<li>{ol_match.group(1)}</li>')
        else:
            if in_ul:
                result_lines.append('</ul>')
                in_ul = False
            if in_ol:
                result_lines.append('</ol>')
                in_ol = False
            if line.strip() == '':
                result_lines.append('<br>')
            else:
                result_lines.append(line)

    if in_ul:
        result_lines.append('</ul>')
    if in_ol:
        result_lines.append('</ol>')

    return '\n'.join(result_lines)


# --- Context Builder ---
def build_context_message(user_text: str, state: dict) -> str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    day = days[now.weekday()]
    time_str = now.strftime(f"%Y.%m.%d, {day}, %H:%M")
    thinking = "🟢" if state["thinking"] else "🔴"
    terminal = "🟢" if state["terminal"] else "🔴"
    return f"{time_str}.\n{thinking} Мышление | {terminal} Терминал\n\n{user_text}"


# --- Keyboard ---
def get_keyboard(state):
    t = f"{'🟢' if state['thinking'] else '🔴'} Мышление"
    m = f"{'🟢' if state['terminal'] else '🔴'} Терминал"
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t), KeyboardButton(text=m)]],
        resize_keyboard=True
    )


# --- Agent Task ---
# --- Safe delivery ---
async def deliver(bot: Bot, chat_id: int, text: str) -> None:
    """Шлёт HTML, режет длинные ответы, при отказе деградирует в plain text."""
    try:
        chunks = split_html(markdown_to_html(text))
    except Exception as e:
        print(f"[md] converter failed: {e}", flush=True)
        chunks = [text]

    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
            continue
        except Exception as e:
            print(f"[send] html rejected: {e}", flush=True)

        plain = strip_markdown(chunk)
        sent = False
        for piece in [plain[i:i + 4096] for i in range(0, len(plain), 4096)] or [plain]:
            try:
                await bot.send_message(chat_id, piece)
                sent = True
            except Exception as e:
                print(f"[send] plain rejected: {e}", flush=True)
        if not sent:
            try:
                await bot.send_message(chat_id, "Не смогла отправить ответ: текст не принимает Telegram.")
            except Exception:
                pass


async def agent_task(bot: Bot, chat_id: int, user_id: int, text: str, state: dict):
    typing_task = None

    async def _typing_loop():
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception as e:
                print(f"[typing] {e}", flush=True)
            await asyncio.sleep(4)

    async def _start_typing():
        nonlocal typing_task
        if typing_task is None or typing_task.done():
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception as e:
                print(f"[typing] {e}", flush=True)
            typing_task = asyncio.create_task(_typing_loop())

    async def _stop_typing():
        nonlocal typing_task
        if typing_task and not typing_task.done():
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
        typing_task = None

    async def on_thinking_start():
        # Только «печатает». Статус-сообщение с таймером удалено по решению Саши:
        # индикатор активности есть, спам в чате не нужен.
        await _start_typing()

    async def send_message(text):
        """Мост для telegram_send: Наташа сама решает, когда что-то сказать по ходу."""
        if not text or not text.strip():
            return "Пустое сообщение не отправлено."
        try:
            await deliver(bot, chat_id, text)
            return "Отправлено."
        except Exception as e:
            return f"Не отправлено: {e}"

    try:
        # Без мышления — индикатор сразу при получении сообщения.
        # С мышлением — только когда реально полетели reasoning-чанки (on_thinking_start).
        if not state.get("thinking", True):
            await _start_typing()
        final_reply = await chat_with_agent(
            user_id=user_id,
            user_text=text,
            on_thinking_start=on_thinking_start,
            thinking=state.get("thinking", True),
            terminal=state.get("terminal", False),
            send_message=send_message,
        )

        if not final_reply.strip():
            final_reply = "Модель вернула пустоту."

        await _stop_typing()
        await deliver(bot, chat_id, final_reply)

    except asyncio.CancelledError:
        await _stop_typing()
        raise
    except Exception as e:
        await _stop_typing()
        try:
            await bot.send_message(chat_id, f"Ошибка агента: {e}")
        except Exception:
            pass


# --- Handlers ---
router = Router()


@router.message(Command("start"), IsOwner())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    used = get_used(uid)
    avail = [i for i in range(len(PHRASES)) if i not in used]
    if not avail:
        reset_used(uid)
        avail = list(range(len(PHRASES)))
    idx = random.choice(avail)
    mark_used(uid, idx)
    state = get_state(uid)
    await message.answer(PHRASES[idx], reply_markup=get_keyboard(state))


@router.message(IsOwner(), lambda m: m.text in ("🟢 Мышление", "🔴 Мышление", "🟢 Терминал", "🔴 Терминал"))
async def toggle_buttons(message: types.Message):
    uid = message.from_user.id
    text = message.text

    if "Мышление" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, thinking=new_val)
    elif "Терминал" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, terminal=new_val)
    else:
        state = get_state(uid)

    await message.answer("Настройки обновлены", reply_markup=get_keyboard(state))


@router.message(IsOwner())
async def forward_to_agent(message: types.Message):
    uid = message.from_user.id

    if uid in _active_tasks and not _active_tasks[uid].done():
        _active_tasks[uid].cancel()
        try:
            await _active_tasks[uid]
        except asyncio.CancelledError:
            pass

    state = get_state(uid)
    context_msg = build_context_message(message.text, state)
    task = asyncio.create_task(agent_task(message.bot, message.chat.id, uid, context_msg, state))
    _active_tasks[uid] = task


# --- Main ---
async def main():
    init_db()
    init_chat_history()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
_ALLOWED={"b","i","u","s","del","ins","em","strong","a","code","pre","blockquote","tg-spoiler"}
_TG=re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")
_SWAP=[("<ul>",""),("</ul>",""),("<ol>",""),("</ol>",""),("</li>","\n"),("<li>","• "),("<br>","\n"),("<hr>","\n\n")]
def _tg_safe(t):
    for a,b in _SWAP:
        t=t.replace(a,b)
    return t.strip()
def strip_markdown(text):
    text=re.sub(r"```[\w+-]*\n?","",text)
    text=re.sub(r"```\s*","",text)
    text=re.sub(r"^#{1,6}\s+","",text,flags=re.M)
    text=re.sub(r"^>\s?","",text,flags=re.M)
    text=re.sub(r"\[([^\]]+)\]\([^)]*\)",r"\1",text)
    text=re.sub(r"\*\*\*|\*\*|__|~~|\|\|","",text)
    text=text.replace("`","")
    return re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)",r"\1",text)
def split_html(text,limit=4096):
    text=_tg_safe(text)
    if len(text)<=limit: return [text]
    out=[];cur=""
    for p in re.split(r"\n\s*\n",text):
        c=p if not cur else cur+"\n\n"+p
        if len(c)<=limit: cur=c;continue
        if cur: out.append(cur)
        cur=""
        for j in range(0,len(p),limit):
            s=p[j:j+limit]
            if j+limit<len(p): out.append(s)
            else: cur=s
    if cur.strip(): out.append(cur)
    return [x for x in out if x.strip()]
