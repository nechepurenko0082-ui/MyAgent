import asyncio
import os
import json
import sqlite3
import sys
from pathlib import Path
from openai import AsyncOpenAI
from dotenv import load_dotenv

import tools

# Загружаем .env из той же папки, где лежит natasha.py
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Конфигурация
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3.8-flash")

# Проверка, что всё на месте
if not AITUNNEL_API_KEY:
    raise ValueError("Проверь .env: AITUNNEL_API_KEY")

# OpenAI клиент для AITunnel
client = AsyncOpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url="https://api.aitunnel.ru/v1/",
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.txt"

if not SYSTEM_PROMPT_FILE.is_file():
    raise ValueError("Создай system_prompt.txt рядом с natasha.py")

SYSTEM_PROMPT = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()

# Инструменты
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Выполняет bash-команду на сервере. Возвращает stdout и stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Команда для выполнения"}
                },
                "required": ["command"]
            }
        }
    }
] + [
    {
        "type": "function",
        "function": {
            "name": "telegram_send",
            "description": (
                "Отправляет Саше сообщение прямо сейчас, посреди работы — не дожидаясь финального ответа. "
                "Используй, когда есть что сказать по делу: нашёл проблему, нужно его решение, "
                "или работа затянется и надо доложить промежуточный итог. "
                "Не используй для отчётов «я начала делать X» и прочей воды: "
                "он и так видит индикатор печати. Одна мысль — одно сообщение, коротко."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст сообщения"}
                },
                "required": ["text"],
            },
        },
    }
] + tools.TOOLS_TOOLS

ALWAYS_TOOLS = {"telegram_send"}


# --- Гибридная память (без лимитов) ---
DB_PATH = BASE_DIR / "chat_history.db"
_history_cache: dict[int, list[dict]] = {}


def init_chat_history():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                 )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id)')
    conn.commit()
    conn.close()


def load_history_from_db(user_id: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, content, tool_calls, tool_call_id FROM chat_history WHERE user_id=? ORDER BY id",
        (user_id,)
    )
    rows = c.fetchall()
    conn.close()

    history = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg = {"role": role}
        if content is not None:
            msg["content"] = content
        if tool_calls_json:
            msg["tool_calls"] = json.loads(tool_calls_json)
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        history.append(msg)

    _history_cache[user_id] = history
    return history


def get_history(user_id: int) -> list[dict]:
    if user_id not in _history_cache:
        load_history_from_db(user_id)
    return _history_cache[user_id]


def save_message(user_id: int, message: dict):
    # Сначала убеждаемся, что история загружена из БД.
    # Иначе первый вызов после рестарта создаёт пустой кэш,
    # а get_history() больше в базу не заглянет.
    get_history(user_id)
    _history_cache[user_id].append(message)

    content = message.get("content")
    tool_calls = json.dumps(message.get("tool_calls")) if message.get("tool_calls") else None
    tool_call_id = message.get("tool_call_id")
    role = message["role"]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO chat_history (user_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, role, content, tool_calls, tool_call_id)
    )
    conn.commit()
    conn.close()


def clear_history(user_id: int):
    _history_cache.pop(user_id, None)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def build_messages_with_cache(history: list[dict]) -> list[dict]:
    """
    Строит сообщения с cache_control для Qwen.
    Кэшируем системный промпт + всю историю кроме последнего сообщения.
    """
    messages = []

    # Системный промпт — всегда кэшируем
    messages.append({
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    })

    # Собираем историю без системного промпта
    non_system = [msg for msg in history if msg.get("role") != "system"]

    if not non_system:
        return messages

    # Все сообщения кроме последнего — стабильный префикс
    for msg in non_system[:-1]:
        messages.append(msg)

    # Предпоследнее сообщение помечаем cache_control,
    # чтобы закэшировать весь префикс до него
    if len(messages) > 1:
        last_cached = messages[-1]
        content = last_cached.get("content", "")
        if isinstance(content, str) and content:
            last_cached["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"}
                }
            ]

    # Последнее сообщение — новое, без кэша
    messages.append(non_system[-1])

    return messages


async def run_command_async(command: str) -> str:
    """Асинхронное выполнение команды без таймаута."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace").strip()
        return output if output else "(команда выполнена, вывод пустой)"
    except Exception as e:
        return f"Ошибка выполнения: {e}"


async def chat_with_agent(user_id: int, user_text: str, on_thinking_start=None, on_tool_call=None, on_text=None, thinking: bool = True, terminal: bool = False, send_message=None) -> str:
    """
    Streaming цикл агента с гибридной памятью. Без лимитов итераций.

    Args:
        user_id: ID юзера
        user_text: Сообщение от юзера (уже с контекстом времени/статусов)
        on_thinking_start: async callback() — вызывается при первом chunk от API
        on_tool_call: async callback(tool_name, args) — при каждом вызове инструмента

    Returns:
        Финальный текстовый ответ агента
    """
    save_message(user_id, {"role": "user", "content": user_text})

    thinking_started = False

    while True:
        history = get_history(user_id)
        messages = build_messages_with_cache(history)

        # telegram_send — это мой голос, он доступен всегда.
        # Доступ к системе (терминал + файлы) — только при включённом тумблере.
        active_tools = [t for t in TOOLS if t["function"]["name"] in ALWAYS_TOOLS]
        if terminal:
            active_tools += [t for t in TOOLS if t not in active_tools]
        req_kwargs = {"tools": active_tools, "tool_choice": "auto"}

        stream = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            stream=True,
            extra_body={"enable_thinking": bool(thinking), "provider": {"sort": "price"}},
            **req_kwargs,
        )

        content_parts = []
        tool_calls_data = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta

            # reasoning прилетает раньше обычного текста — считаем его началом раздумья
            reasoning_chunk = getattr(delta, "reasoning", None)
            if not thinking_started and isinstance(reasoning_chunk, str) and reasoning_chunk:
                thinking_started = True
                if on_thinking_start:
                    await on_thinking_start()

            # Первый chunk с контентом или tool_calls = модель начала думать
            if not thinking_started and (delta.content or delta.tool_calls):
                thinking_started = True
                if on_thinking_start:
                    await on_thinking_start()

            if delta.content:
                content_parts.append(delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_data[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_data[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_data[idx]["arguments"] += tc_delta.function.arguments

        final_content = "".join(content_parts)

        # Формируем сообщение ассистента для сохранения
        msg_dict = {"role": "assistant", "content": final_content}
        if tool_calls_data:
            sorted_indices = sorted(tool_calls_data.keys())
            msg_dict["tool_calls"] = [
                {
                    "id": tool_calls_data[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tool_calls_data[i]["name"],
                        "arguments": tool_calls_data[i]["arguments"]
                    }
                }
                for i in sorted_indices
            ]
        save_message(user_id, msg_dict)

        # Если нет tool_calls — финальный ответ
        if not tool_calls_data:
            return final_content

        # Промежуточный текст (комментарий перед командой) отправляем в чат
        if final_content.strip() and tool_calls_data and on_text:
            await on_text(final_content)
        # Обрабатываем tool_calls
        for idx in sorted(tool_calls_data.keys()):
            tc_data = tool_calls_data[idx]
            name = tc_data["name"]
            try:
                args = json.loads(tc_data["arguments"])
            except json.JSONDecodeError as e:
                tool_result = f"Ошибка парсинга аргументов: {e}"
                save_message(user_id, {"role": "tool", "tool_call_id": tc_data["id"], "content": tool_result})
                continue

            if on_tool_call:
                await on_tool_call(name, args)

            if name == "run_command":
                command = args.get("command", "")
                if not terminal:
                    tool_result = "Терминал недоступен: тумблер выключен пользователем."
                else:
                    tool_result = await run_command_async(command)
            elif name == "telegram_send":
                if send_message is None:
                    tool_result = "Канал отправки не подключён."
                else:
                    try:
                        tool_result = await send_message(args.get("text", ""))
                    except Exception as e:
                        tool_result = f"Не отправил: {type(e).__name__}: {e}"
            elif name in tools.DISPATCH:
                # Файловые инструменты под тем же тумблером, что и терминал:
                # иначе это обход блокировки, а не инструмент.
                if not terminal:
                    tool_result = "Файловые инструменты недоступны: тумблер выключен пользователем."
                else:
                    try:
                        tool_result = await asyncio.to_thread(tools.DISPATCH[name], **args)
                    except TypeError as e:
                        tool_result = f"Неверные аргументы для {name}: {e}"
                    except Exception as e:
                        tool_result = f"Ошибка {name}: {type(e).__name__}: {e}"
            else:
                tool_result = f"Неизвестный инструмент: {name}"

            save_message(user_id, {"role": "tool", "tool_call_id": tc_data["id"], "content": tool_result})

        # Сбрасываем флаг для следующей итерации (агент может думать снова после tool)
        thinking_started = False


# Автоматический запуск bot.py при импорте/запуске agent.py
if __name__ == "__main__":
    sys.modules.setdefault("agent", sys.modules[__name__])
    import bot
    asyncio.run(bot.main())
