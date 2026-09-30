import asyncio
import os
import random
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command, Filter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.enums import ChatAction, ParseMode
from html import escape as hescape
from phrases import PHRASES
from agent import chat_with_agent, init_chat_history, clear_history
import tools

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
if not BOT_TOKEN or not ALLOWED_USER_ID:
    raise ValueError("BOT_TOKEN и ALLOWED_USER_ID должны быть в .env")

DB_PATH = str(Path(__file__).resolve().parent / "user_states.db")

# Очередь входящих: бот принимает всё подряд, агент разбирает по одному.
# Новое сообщение больше не отменяет текущую работу — она продолжается.
# Раздельные очереди: владелец — всегда вне очереди (приоритет),
# остальные между собой — строго по времени (кто первый, тот первый).
_queue_owner: list = []
_queue_users: list = []             # общий FIFO: порядок добавления = порядок по времени
_worker: asyncio.Task | None = None
_bot: "Bot | None" = None


def _queue_put(item: dict):
    """Кладёт сообщение в правильную очередь: владелец — в приоритетную."""
    if item["chat_id"] == ALLOWED_USER_ID:
        _queue_owner.append(item)
    else:
        _queue_users.append(item)


def _queue_take():
    """Забирает следующее: сначала владелец, потом пользователи по времени."""
    if _queue_owner:
        return _queue_owner.pop(0)
    if _queue_users:
        return _queue_users.pop(0)
    return None


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
                    terminal INTEGER DEFAULT 0,
                    price INTEGER DEFAULT 0,
                    users INTEGER DEFAULT 1,
                    search INTEGER DEFAULT 1,
                    speech INTEGER DEFAULT 0)''')
    # Миграция старых БД: колонок price/users/search/speech ещё нет
    cols = [r[1] for r in c.execute("PRAGMA table_info(user_states)").fetchall()]
    if "price" not in cols:
        c.execute("ALTER TABLE user_states ADD COLUMN price INTEGER DEFAULT 0")
    if "users" not in cols:
        c.execute("ALTER TABLE user_states ADD COLUMN users INTEGER DEFAULT 1")
    if "search" not in cols:
        c.execute("ALTER TABLE user_states ADD COLUMN search INTEGER DEFAULT 1")
    if "speech" not in cols:
        c.execute("ALTER TABLE user_states ADD COLUMN speech INTEGER DEFAULT 0")
    # Пользователи бота: заявки и допуск
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    status TEXT DEFAULT 'pending')''')
    # Владелец — всегда одобрен, имя зафиксировано
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, display_name, status) "
        "VALUES (?, NULL, 'Alexander', 'approved')",
        (ALLOWED_USER_ID,),
    )
    conn.commit()
    conn.close()


def get_user(user_id):
    """Запись о пользователи: username, display_name, status. None — неизвестен."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, display_name, status FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return {"username": row[0], "display_name": row[1], "status": row[2]}


def create_request(user_id, username):
    """Ставит заявку в pending (если ещё нет) и возвращает статус."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO users (user_id, username, status) VALUES (?, ?, 'pending')",
            (user_id, username),
        )
        status = "pending"
    else:
        status = row[0]
    conn.commit()
    conn.close()
    return status


def set_user_status(user_id, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
    conn.commit()
    conn.close()


def set_user_name(user_id, name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET display_name=? WHERE user_id=?", (name, user_id))
    conn.commit()
    conn.close()


def user_label(user_id):
    u = get_user(user_id) or {}
    return u.get("display_name") or u.get("username") or ("Alexander" if user_id == ALLOWED_USER_ID else f"user{user_id}")


# Короткие имена-алиасы для владельца, которых нет в базе
_OWNER_ALIASES = {"саша", "sasha", "alexander", "создатель", "владелец", "owner"}

# Алиасы реальных имён участников (в базе они под другими именами)
# Алиасы имён -> user_id берутся из .env (USER_ALIASES="имя:id,имя:id"),
# чтобы личные данные не попадали в репозиторий.
def _load_name_aliases():
    out = {}
    for part in (os.getenv("USER_ALIASES") or "").split(","):
        part = part.strip()
        if ":" in part:
            name, uid = part.rsplit(":", 1)
            try:
                out[name.strip().lower()] = int(uid.strip())
            except ValueError:
                pass
    return out


_NAME_ALIASES = _load_name_aliases()


def resolve_user_by_name(name):
    """Имя → user_id. Возвращает int, список id (неоднозначность) или None."""
    raw = (name or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    low = raw.lower().lstrip("@")
    if low in _OWNER_ALIASES:
        return ALLOWED_USER_ID
    if low in _NAME_ALIASES:
        return _NAME_ALIASES[low]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Точное совпадение: display_name или username (без учёта регистра)
    c.execute(
        "SELECT user_id FROM users WHERE lower(coalesce(display_name,''))=? OR lower(coalesce(username,''))=?",
        (low, low),
    )
    rows = [r[0] for r in c.fetchall()]
    if not rows:
        # Нечёткое: начало имени
        c.execute(
            "SELECT user_id FROM users WHERE display_name IS NOT NULL AND lower(display_name) LIKE ?",
            (low + "%",),
        )
        rows = [r[0] for r in c.fetchall()]
    conn.close()
    if not rows:
        return None
    if len(rows) > 1:
        return rows
    return rows[0]


def effective_state(user_id):
    """
    Единый контекст: тумблеры (мышление, цена) — всегда владельца, у всех общие.
    Терминал жёстко только у владельца, даже если у него тумблер включён.
    """
    st = dict(get_state(ALLOWED_USER_ID))
    st["terminal"] = bool(st["terminal"]) and user_id == ALLOWED_USER_ID
    return st


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
    c.execute(
        "SELECT thinking, terminal, price, users, search, speech FROM user_states WHERE user_id=?",
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "thinking": bool(row[0]),
            "terminal": bool(row[1]),
            "price": bool(row[2]),
            "users": bool(row[3]),
            "search": bool(row[4]) if len(row) > 4 else True,
            "speech": bool(row[5]) if len(row) > 5 else False,
        }
    return {"thinking": True, "terminal": False, "price": False, "users": True, "search": True, "speech": False}


def set_state(user_id, thinking=None, terminal=None, price=None, users=None, search=None, speech=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    current = get_state(user_id)
    new_thinking = thinking if thinking is not None else current["thinking"]
    new_terminal = terminal if terminal is not None else current["terminal"]
    new_price = price if price is not None else current["price"]
    new_users = users if users is not None else current["users"]
    new_search = search if search is not None else current["search"]
    new_speech = speech if speech is not None else current["speech"]
    c.execute(
        "INSERT INTO user_states (user_id, thinking, terminal, price, users, search, speech) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET thinking=?, terminal=?, price=?, users=?, search=?, speech=?",
        (user_id, int(new_thinking), int(new_terminal), int(new_price), int(new_users), int(new_search), int(new_speech),
         int(new_thinking), int(new_terminal), int(new_price), int(new_users), int(new_search), int(new_speech))
    )
    conn.commit()
    conn.close()
    return {
        "thinking": new_thinking,
        "terminal": new_terminal,
        "price": new_price,
        "users": new_users,
        "search": new_search,
        "speech": new_speech,
    }


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
    in_pre = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            result_lines.append('</ul>')
            in_ul = False
        if in_ol:
            result_lines.append('</ol>')
            in_ol = False

    for line in lines:
        if '<pre>' in line:
            in_pre = True
        in_code = in_pre
        if '</pre>' in line:
            in_pre = False

        if in_code:
            result_lines.append(line)
            continue

        # Заголовки: Telegram не знает <h1>-<h6>, рендерим жирным
        h_match = re.match(r'^#{1,6}\s+(.*)', line)
        if h_match:
            close_lists()
            result_lines.append(f'<b>{h_match.group(1)}</b>')
            continue

        # Горизонтальная линейка: <hr> не в white-list, заменяем пустой строкой
        if re.match(r'^(-{3,}|\*{3,}|_{3,})\s*$', line):
            close_lists()
            result_lines.append('')
            continue

        # Цитаты: <blockquote> разрешен Telegram
        q_match = re.match(r'^&gt;\s?(.*)', line)
        if q_match:
            close_lists()
            result_lines.append(f'<blockquote>{q_match.group(1)}</blockquote>')
            continue

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
def build_context_message(user_text: str, state: dict, user_id: int) -> str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    day = days[now.weekday()]
    time_str = now.strftime(f"%Y.%m.%d, {day}, %H:%M")
    thinking = "🟢" if state["thinking"] else "🔴"
    terminal = "🟢" if state["terminal"] else "🔴"
    users = "🟢" if state.get("users", True) else "🔴"
    search = "🟢" if state.get("search", True) else "🔴"
    speech = "🟢" if state.get("speech", False) else "🔴"
    # Имя без ID: «Alexander:». ID есть в системе (user_id), но в текст не выводим.
    who = f"{user_label(user_id)}:"
    return (
        f"{time_str}.\n"
        f"{thinking} Мышление | {terminal} Терминал | {users} Пользователи | {search} Поиск | {speech} Голос\n"
        f"{who}\n{user_text}"
    )


# --- Keyboard ---
def get_keyboard(state):
    t = f"{'🟢' if state['thinking'] else '🔴'} Мышление"
    m = f"{'🟢' if state['terminal'] else '🔴'} Терминал"
    u = f"{'🟢' if state.get('users', True) else '🔴'} Пользователи"
    s = f"{'🟢' if state.get('search', True) else '🔴'} Поиск"
    v = f"{'🟢' if state.get('speech', False) else '🔴'} Голос"
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t), KeyboardButton(text=m)],
                  [KeyboardButton(text=u), KeyboardButton(text=s)],
                  [KeyboardButton(text=v)]],
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

    async def _speak(target: int, text: str) -> bool:
        """Озвучивает текст (TTS) и шлёт ГОЛОСОВЫМ (send_voice). False = шли текстом."""
        clean = strip_markdown(text)[:4000]
        if not clean.strip():
            return False
        path = await asyncio.to_thread(tools.voice_synth, clean)
        if isinstance(path, str) and path.startswith("ОТКАЗ"):
            print(f"[tts] {path}", flush=True)
            return False
        try:
            await bot.send_voice(target, FSInputFile(path))
            return True
        except Exception as e:
            print(f"[tts] не отправил голосовое: {e}", flush=True)
            return False
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    sent_to_owner = {"done": False}

    async def send_message(text, user_id=None, name=None):
        """
        Мост для telegram_send: инструмент строго для владельца.
        name — имя получателя, ID определяется автоматически по базе users.
        Отметка sent_to_owner нужна, чтобы финальный текст не дублировал
        то, что уже ушло адресно через инструмент.
        """
        if chat_id != ALLOWED_USER_ID:
            return (
                "ЗАПРЕЩЕНО: telegram_send — только для владельца. "
                "Пользователям ответ уходит автоматически."
            )
        # Тумблер «Пользователи» 🔴 — жёсткая блокировка на уровне кода,
        # а не просьба не пользоваться: вызов просто не пройдёт.
        if not state.get("users", True):
            return (
                "ЗАПРЕЩЕНО: тумблер «Пользователи» 🔴 — telegram_send "
                "заблокирован, пока не будет 🟢."
            )
        if not text or not text.strip():
            return "Пустое сообщение не отправлено."
        target = chat_id
        if name:
            resolved = resolve_user_by_name(name)
            if resolved is None:
                return f"Не нашёл получателя по имени «{name}» — ID не определён."
            if isinstance(resolved, list):
                return (
                    f"Имя «{name}» неоднозначно, подходит несколько id: {resolved}. "
                    "Уточни имя или используй user_id."
                )
            target = resolved
        elif user_id:
            target = int(user_id)
        if state.get("speech") and await _speak(target, text):
            if target == ALLOWED_USER_ID:
                sent_to_owner["done"] = True
            return f"Голосовое отправлено в {target}."
        try:
            await deliver(bot, target, text)
            if target == ALLOWED_USER_ID:
                sent_to_owner["done"] = True
            return f"Отправлено в {target}."
        except Exception as e:
            return f"Не отправлено: {e}"

    try:
        # Без мышления — индикатор сразу при получении сообщения.
        # С мышлением — только когда реально полетели reasoning-чанки (on_thinking_start).
        if not state.get("thinking", True):
            await _start_typing()
        final_reply = await chat_with_agent(
            # Единый контекст: история одна на всех, ключ — владцец.
            user_id=ALLOWED_USER_ID,
            user_text=text,
            on_thinking_start=on_thinking_start,
            thinking=state.get("thinking", True),
            terminal=state.get("terminal", False),
            price=state.get("price", False),
            search=state.get("search", True),
            send_message=send_message,
            users=state.get("users", True),
        )

        await _stop_typing()

        if final_reply.strip():
            if chat_id == ALLOWED_USER_ID and sent_to_owner["done"]:
                # Уже ушло адресно через telegram_send — финал не дублируем.
                print(f"[agent] финал шёл через telegram_send: {final_reply[:300]}", flush=True)
            elif not (state.get("speech") and await _speak(chat_id, final_reply)):
                # Новый контракт: финальный текст доставляется автоматически
                # (владелецу и пользователям одинаково).
                await deliver(bot, chat_id, final_reply)
        else:
            print(f"[agent] пустой финальный ответ chat={chat_id}", flush=True)

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


def enqueue(message: types.Message, state: dict, text: str = None):
    """
    Кладёт входящее в очередь агента. Агент получает всё, что происходит в боте,
    и сам решает — отвечать через telegram_send или молчать.
    Текущая работа при новом сообщении не отменяется: сообщения ждут в очереди.
    """
    global _worker
    raw = text if text is not None else (message.text or message.caption or "")
    if not raw.strip():
        raw = f"[{message.content_type}]"
    _queue_put({
        "bot": message.bot,
        "chat_id": message.chat.id,
        "uid": message.from_user.id,
        "text": build_context_message(raw, state, message.from_user.id),
        "state": state,
    })
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_worker_loop())


def enqueue_event(text: str, context_uid: int, chat_id: int = None):
    """
    Системное событие для агента (заявка на регистрацию и т.п.).
    Контекст строится от имени context_uid, но доставка по умолчанию — владельцу
    (chat_id = его ID), чтобы уведомление шло туда, куда нужно.
    """
    global _worker
    owner_id = ALLOWED_USER_ID
    _queue_put({
        "bot": None,
        "chat_id": owner_id if chat_id is None else chat_id,
        "uid": context_uid,
        "text": build_context_message(text, effective_state(owner_id), context_uid),
        "state": effective_state(owner_id),
    })
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_worker_loop())


async def _worker_loop():
    """Разбирает очередь по одному: владелец в приоритете, остальные по времени."""
    while True:
        item = _queue_take()
        if item is None:
            return
        try:
            await agent_task(
                item["bot"] or _bot, item["chat_id"], item["uid"],
                item["text"], item["state"],
            )
        except Exception as e:
            print(f"[queue] ошибка обработки: {e}", flush=True)


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id

    if uid == ALLOWED_USER_ID:
        used = get_used(uid)
        avail = [i for i in range(len(PHRASES)) if i not in used]
        if not avail:
            reset_used(uid)
            avail = list(range(len(PHRASES)))
        idx = random.choice(avail)
        mark_used(uid, idx)
        state = get_state(uid)
        await message.answer(PHRASES[idx], reply_markup=get_keyboard(state))
        return

    user = get_user(uid)
    if user is None or user["status"] == "pending":
        status = create_request(uid, message.from_user.username)
        if status == "pending":
            who = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
            enqueue_event(
                f"ЗАЯВКА НА РЕГИСТРАЦИЮ: {uid}, {who}, "
                f"имя в профиле: {message.from_user.full_name}. "
                f"Ждёт твоего решения. Одобрить = обновить users.status на 'approved' "
                f"(sql: UPDATE users SET status='approved' WHERE user_id={uid}; "
                f"затем написать ему через telegram_send с user_id={uid} и попросить имя). "
                f"Отклонить = status='denied'.",
                context_uid=uid,
            )
            await message.answer("Заявка отправлена, ждите решения.")
        else:
            await message.answer("Заявка уже на рассмотрении.")
        return

    if user["status"] == "denied":
        await message.answer("В доступе отказано.")
        return

    # approved
    if not user["display_name"]:
        await message.answer("Регистрация одобрена! Как тебя зовут?")
    else:
        await message.answer(f"Привет, {user['display_name']}!")


@router.message(IsOwner(), lambda m: m.text in (
    "🟢 Мышление", "🔴 Мышление",
    "🟢 Терминал", "🔴 Терминал",
    "🟢 Пользователи", "🔴 Пользователи",
    "🟢 Поиск", "🔴 Поиск",
    "🟢 Голос", "🔴 Голос",
))
async def toggle_buttons(message: types.Message):
    uid = message.from_user.id
    text = message.text

    if "Мышление" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, thinking=new_val)
    elif "Терминал" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, terminal=new_val)
    elif "Пользователи" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, users=new_val)
    elif "Поиск" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, search=new_val)
    elif "Голос" in text:
        new_val = text.startswith("🔴")
        state = set_state(uid, speech=new_val)
    else:
        state = get_state(uid)

    await message.answer("Настройки обновлены", reply_markup=get_keyboard(state))


@router.message(Command("price"), IsOwner())
async def cmd_price(message: types.Message):
    uid = message.from_user.id
    raw = (message.text or "").split(maxsplit=1)
    arg = raw[1].strip().lower() if len(raw) > 1 else "status"

    if arg in ("on", "off"):
        state = set_state(uid, price=(arg == "on"))
        if state["price"]:
            await message.answer(
                "💰 /price on — теперь выбираю самый дешёвый провайдер.",
                reply_markup=get_keyboard(state),
            )
        else:
            await message.answer(
                "⚡ /price off — дефолт провайдера, минимальная задержка.",
                reply_markup=get_keyboard(state),
            )
        return

    if arg in ("status", "статус"):
        state = get_state(uid)
        if state["price"]:
            text = "💰 Провайдер: сортировка по цене (дешёвый)."
        else:
            text = "⚡ Провайдер: дефолт (минимальная задержка)."
        await message.answer(text, reply_markup=get_keyboard(state))
        return

    state = get_state(uid)
    await message.answer(
        "Формат: /price on | /price off | /price status",
        reply_markup=get_keyboard(state),
    )


@router.message(Command("clear"), IsOwner())
async def cmd_clear(message: types.Message):
    """Полная очистка контекста: история (общая на всех) стирается из БД и кэша."""
    clear_history(ALLOWED_USER_ID)
    _queue_owner.clear()
    _queue_users.clear()
    await message.answer("🧹 Контекст очищен.")


@router.message()
async def forward_to_agent(message: types.Message):
    uid = message.from_user.id

    # Владелец — всегда в очередь.
    if uid == ALLOWED_USER_ID:
        enqueue(message, effective_state(uid))
        return

    user = get_user(uid)
    if user is None or user["status"] == "pending":
        await message.answer("Напиши /start, чтобы подать заявку на регистрацию.")
        return
    if user["status"] == "denied":
        await message.answer("В доступе отказано.")
        return

    # «Пользователи» 🔴 — сообщения пользователей вообще не доходят до агента:
    # ни в очередь, ни на обработку, ни на расход токенов. Тишина.
    if not effective_state(uid).get("users", True):
        return

    # Одобрен, но имени ещё нет: первое сообщение = имя.
    if not user["display_name"]:
        name = (message.text or "").strip()[:64]
        if not name:
            await message.answer("Напиши своё имя текстом.")
            return
        set_user_name(uid, name)
        await message.answer(f"Приятно познакомиться, {name}! 👋")
        enqueue(
            message,
            effective_state(uid),
            text=f"[ПОЛЬЗОВАТЕЛЬ ВВЁЛ ИМЯ: {name}]",
        )
        return

    enqueue(message, effective_state(uid))


# --- Main ---
async def reminder_loop(bot: Bot):
    """Фоновый планировщик: раз в 30 сек проверяет напоминания и доставляет."""
    from datetime import datetime as _dt
    while True:
        try:
            now_iso = _dt.now(ZoneInfo("Europe/Moscow")).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
            for rid, chat_id, text in tools.reminders_due(now_iso):
                # Все напоминания приходят только владельцу и ими управляет только он.
                try:
                    await deliver(bot, ALLOWED_USER_ID, f"⏰ Напоминание: {text}")
                except Exception as e:
                    # Ошибка доставки НЕ удаляет напоминание: остаётся в БД
                    # и повторится на следующем круге (каждые 30 секунд),
                    # пока доставка не пройдёт успешно.
                    print(f"[reminder] не доставил #{rid}: {e} — повтор позже", flush=True)
                    continue
                tools.reminders_forget(rid)
        except Exception as e:
            print(f"[reminder] ошибка планировщика: {e}", flush=True)
        await asyncio.sleep(30)


async def main():
    global _bot
    init_db()
    init_chat_history()
    _bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(reminder_loop(_bot))
    print("Бот запущен.")
    await dp.start_polling(_bot)


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


if __name__ == "__main__":
    asyncio.run(main())
