# tools.py — файловые инструменты Наташи
#
# Четыре инструмента вместо голого bash. Главные принципы:
#   1. edit_file требует РОВНО одно совпадение old_text. Иначе — отказ, файл не тронут.
#   2. Любая запись в существующий файл идёт с бэкапом рядом (backups/).
#   3. write_file по умолчанию create_only: новый файл, а не затыривание старого.
#   4. search_code сам выкидывает мусор (venv, backups, __pycache__, .bak),
#      чтобы «найдено 6 совпадений» не означало «4 из них в бэкапах».
#   5. Всё только внутри /home/sasha. За пределы не пускаю.

import difflib
import os
import re
import shutil
import time
from pathlib import Path

HOME = Path("/home/sasha")
BACKUP_DIR = HOME / "myagent" / "backups"

# Что никогда не читаем и не ищем
JUNK_DIRS = {"venv", ".venv", "__pycache__", "node_modules", ".git", "backups"}
JUNK_SUFFIXES = (".bak", ".pyc", ".db-wal", ".db-shm", ".db", ".tar.gz", ".log")
JUNK_PATTERNS = (".pre-", ".removed.", ".bak-")

MAX_SEARCH_HITS = 50
MAX_PER_FILE = 8
MAX_READ_BYTES = 400_000
MAX_READ_BYTES_OUT = 9_000
MAX_TOOL_OUTPUT = 12_000


# ---------- утилиты ----------

def _err(msg: str) -> str:
    return f"ОТКАЗ: {msg}"


def _resolve(path: str) -> Path:
    """Относительный путь считаем от текущего каталога процесса, как в bash.
    Песочница при этом остаётся HOME: наружу не выпускаем."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _safe_read(path: str):
    p = _resolve(path)
    if HOME not in p.parents and p != HOME:
        return None, _err(f"путь вне {HOME}: {p}")
    return p, None


def _safe_write(path: str):
    p, e = _safe_read(path)
    if e:
        return None, None, e
    if p.is_dir():
        return None, None, _err(f"это директория, не файл: {p}")
    if not p.exists():
        parent = p.parent
        if HOME not in parent.parents and parent != HOME:
            return None, None, _err(f"путь вне {HOME}: {p}")
    return p, _backup(p), None


def _backup(p: Path):
    """Бэкап перед записью. Возвращает путь к бэкапу или None."""
    if not p.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    dst = BACKUP_DIR / f"{p.name}.{stamp}.pre-write.bak"
    n = 1
    while dst.exists():
        dst = BACKUP_DIR / f"{p.name}.{stamp}_{n}.pre-write.bak"
        n += 1
    shutil.copy2(p, dst)
    return dst


def _diff(old: str, new: str, name: str, context: int = 3) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=context,
    ))
    if not lines:
        return "(изменений нет)"
    out = "".join(lines)
    if len(out) > 6000:
        out = out[:6000] + "\n...[diff обрезан]"
    return out


def _is_junk(p: Path) -> bool:
    if any(part in JUNK_DIRS for part in p.parts):
        return True
    if p.suffix in JUNK_SUFFIXES:
        return True
    if any(pat in p.name for pat in JUNK_PATTERNS):
        return True
    return False


# ---------- read_file ----------

def read_file(path: str, start_line=None, end_line=None) -> str:
    p, e = _safe_read(path)
    if e:
        return e
    if not p.exists():
        return _err(f"файл не найден: {p}")
    if not p.is_file():
        return _err(f"не файл: {p}")

    try:
        start = int(start_line) if start_line else 1
    except (TypeError, ValueError):
        start = 1
    try:
        end = int(end_line) if end_line else None
    except (TypeError, ValueError):
        end = None

    size = p.stat().st_size
    if size > MAX_READ_BYTES and end is None:
        # Без диапазона большой файл читать бессмысленно (и опасно по памяти).
        # С диапазоном — читаем: вывод всё равно ограничен MAX_READ_BYTES_OUT.
        return _err(
            f"файл {size} байт, лимит {MAX_READ_BYTES}. "
            "Укажи start_line/end_line — тогда прочитаю нужный кусок."
        )

    truncated_range = False
    big_file = size > MAX_READ_BYTES
    if big_file:
        # Большой файл не грузим целиком: один проход, в память — только диапазон.
        start = max(1, start)
        lines = []
        total = 0
        with p.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                total = i
                if start <= i <= end:
                    if len(lines) < 20_000:
                        lines.append(line.rstrip("\n"))
                    else:
                        truncated_range = True
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return _err(f"не прочитал: {exc}")
        lines = text.splitlines()
        total = len(lines)

    start = max(1, start)
    end = total if end is None else min(total, end)
    if start > end:
        return _err(f"start_line {start} > end_line {end}")

    # Режем по байтам, но строго по границам строк: обрубленная посередине
    # строка хуже лимита — она выглядит как конец файла.
    # В большой ветке lines уже начинается с start — там срез иной.
    chunk = lines[:end - start + 1] if big_file else lines[start - 1:end]
    width = len(str(end))
    out, shown, used = [], 0, 0
    for i, line in enumerate(chunk, start):
        row = f"{i:>{width}}| {line}"
        used += len(row) + 1
        if used > MAX_READ_BYTES_OUT and shown:
            break
        out.append(row)
        shown += 1

    last_shown = start + shown - 1
    head = f"{p} | строк всего: {total} | показано {start}-{last_shown}"
    if last_shown < end or truncated_range:
        head += f" | ОБРЕЗАНО, продолжение: start_line={last_shown + 1}"
    return head + "\n" + "\n".join(out)


# ---------- edit_file ----------

def edit_file(path: str, old_text: str, new_text: str) -> str:
    if not old_text:
        return _err("old_text пуст — это не замена, а гадание.")
    if old_text == new_text:
        return _err("old_text и new_text одинаковые.")

    p, e = _safe_read(path)
    if e:
        return e
    if not p.exists():
        return _err(f"файла нет: {p} — для нового файла бери write_file")
    if p.is_dir():
        return _err(f"не файл: {p}")

    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        return _err(f"не прочитал: {exc}")

    count = text.count(old_text)
    if count == 0:
        sample = old_text.splitlines()[0][:80]
        return _err(f"не нашёл old_text в {p.name}. Первая строка поиска: {sample!r}")
    if count > 1:
        pos = []
        i = text.find(old_text)
        while i != -1 and len(pos) < 10:
            pos.append(text.count("\n", 0, i) + 1)
            i = text.find(old_text, i + 1)
        return _err(
            f"old_text встречается {count} раз в {p.name} (строки {pos}). "
            "Расширь контекст, чтобы совпадение было ровно одно. Файл не тронут."
        )

    new_content = text.replace(old_text, new_text, 1)
    try:
        bak = _backup(p)
        p.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        return _err(f"не записал: {exc}")

    res = (f"OK: {p.name}, 1 замена"
           + (f", бэкап: {bak.name}" if bak else "") + "\n"
           + _diff(text, new_content, p.name))
    return res


# ---------- write_file ----------

def write_file(path: str, content: str, create_only: bool = True) -> str:
    p, bak, e = _safe_write(path)
    if e:
        return e

    if p.exists():
        if create_only:
            return _err(
                f"{p} уже существует (create_only=true). "
                "Либо edit_file для правки, либо create_only=false осознанно."
            )
        old = p.read_text(encoding="utf-8", errors="replace")
        try:
            p.write_text(content, encoding="utf-8")
        except Exception as exc:
            return _err(f"не записал: {exc}")
        return (f"OK: перезаписан {p.name}, {len(content)} байт"
                + (f", бэкап: {bak.name}" if bak else "") + "\n"
                + _diff(old, content, p.name, context=1))

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return _err(f"не создал: {exc}")
    return f"OK: создан {p}, {len(content)} байт, {content.count(chr(10)) + 1} строк"


# ---------- search_code ----------

def search_code(pattern: str, path: str = None, glob: str = None,
                fixed: bool = False, ignore_junk: bool = True,
                context: int = 0, limit: int = MAX_SEARCH_HITS) -> str:
    if not pattern:
        return _err("пустой pattern")

    root, e = _safe_read(path or str(HOME))
    if e:
        return e
    if not root.exists():
        return _err(f"нет пути: {root}")
    if root.is_file():
        files = [root]
    else:
        gx = None
        if glob:
            gx = re.compile(glob.replace(".", "\\.").replace("*", ".*").replace("?", "."))
        files = []
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            if ignore_junk and _is_junk(f):
                continue
            if gx and not gx.search(f.name):
                continue
            files.append(f)
            if len(files) > 20000:
                break

    if fixed:
        rx = re.compile(re.escape(pattern))
    else:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return _err(f"кривой regex: {exc}")

    try:
        ctx = max(0, min(int(context), 5))
    except (TypeError, ValueError):
        ctx = 0

    hits = []
    skipped = 0
    for f in files:
        if len(hits) >= limit:
            break
        try:
            if f.stat().st_size > 2_000_000:
                skipped += 1
                continue
            text = f.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError, PermissionError):
            continue
        lines = text.splitlines()
        per_file = 0
        for n, line in enumerate(lines, 1):
            if len(hits) >= limit or per_file >= MAX_PER_FILE:
                break
            if rx.search(line):
                per_file += 1
                block = [f"{f}:{n}:{line}"]
                if ctx:
                    for j in range(max(0, n - 1 - ctx), min(len(lines), n + ctx)):
                        if j + 1 != n:
                            block.append(f"{f}:{j+1}  {lines[j]}")
                hits.append("\n".join(block))
        if len(hits) >= limit:
            break

    if not hits:
        return f"Ничего не найдено: {pattern!r} в {root}" + (f" (glob={glob})" if glob else "")

    more = len(hits) >= limit
    out = "\n".join(hits)
    if len(out) > 12000:
        out = out[:12000] + "\n...[вывод обрезан]"
    head = f"Найдено {len(hits)} совпадений" + (" (достигнут лимит, возможно есть ещё)" if more else "")
    if skipped:
        head += f". Пропущено больших файлов: {skipped}"
    return f"{head}\n{out}"


# ---------- долговременная память (всегда доступна) ----------
#
# Память — не серверная команда, а «мозги» агента: работает даже при
# выключенном терминале. Файлы здесь не трогаем, только своя БД.

import sqlite3

MEMORY_DB = HOME / "myagent" / "memory.db"


def _mem_conn():
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        fact TEXT NOT NULL,
                        ts DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mem_user ON facts(user_id)')
    conn.commit()
    return conn


def memory_save(user_id: int, fact: str) -> str:
    fact = (fact or "").strip()
    if not fact:
        return _err("пустой факт — нечего сохранять")
    if len(fact) > 2000:
        return _err(f"слишком длинно ({len(fact)} символов), сократи до сути")
    conn = _mem_conn()
    # Дедупликация: точный повтор не плодим
    dup = conn.execute(
        "SELECT id FROM facts WHERE user_id=? AND fact=?", (int(user_id), fact)
    ).fetchone()
    if dup:
        conn.close()
        return f"OK: уже было в памяти (запись #{dup[0]}), не стал дублировать."
    cur = conn.execute(
        "INSERT INTO facts (user_id, fact) VALUES (?, ?)", (int(user_id), fact)
    )
    conn.commit()
    conn.close()
    return f"OK: сохранил в память (запись #{cur.lastrowid}): {fact[:120]}"


def memory_load(user_id: int, limit: int = 20) -> str:
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    conn = _mem_conn()
    rows = conn.execute(
        "SELECT fact, ts FROM facts WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (int(user_id), limit)
    ).fetchall()
    conn.close()
    if not rows:
        return f"Память по user_id={user_id} пуста."
    out = "\n".join(f"- [{ts}] {fact}" for fact, ts in reversed(rows))
    return f"{len(rows)} фактов, хронологически (новые в конце):\n{out}"


def memory_forget(user_id: int, fact: str) -> str:
    fact = (fact or "").strip()
    if not fact:
        return _err("что именно забыть? Передай текст факта точь-в-точь.")
    conn = _mem_conn()
    cur = conn.execute(
        "DELETE FROM facts WHERE user_id=? AND fact=?", (int(user_id), fact)
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return _err("такого факта в памяти нет (нужно точное совпадение текста).")
    return f"OK: удалил записей: {cur.rowcount}"


def memory_reminder(user_id) -> str | None:
    """Строка-напоминание для истории, если память по юзеру пуста."""
    try:
        conn = _mem_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
        conn.close()
    except Exception:
        return None
    if n:
        return None
    return ("Напоминание о памяти: инструменты memory_save/memory_load/memory_forget "
            "доступны ВСЕГДА, даже когда терминал выключен. Сохраняй сюда важные факты "
            "о людях, решения и договорённости, чтобы помнить их между диалогами.")


# ---------- интернет (всегда доступен) ----------

import html as _html
import json as _json
import re as _re
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Fryd/1.0"}
_HTTP_TIMEOUT = 12


def web_search(query: str, limit: int = 5) -> str:
    """Поиск через DuckDuckGo lite. Возвращает title, url, snippet."""
    query = (query or "").strip()
    if not query:
        return _err("пустой запрос")
    try:
        limit = max(1, min(int(limit), 8))
    except (TypeError, ValueError):
        limit = 5
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(
        "https://lite.duckduckgo.com/lite/", data=data,
        headers={**_UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        body = urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT).read().decode("utf-8", "replace")
    except Exception as e:
        return f"Поиск недоступен: {type(e).__name__}: {e}"

    results = []
    # href в DDG lite идёт с двойными кавычками, класс — с одинарными.
    for m in _re.finditer(
        r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>"
        r"(.*?)</tr>",
        body, _re.DOTALL,
    ):
        if len(results) >= limit:
            break
        url = _html.unescape(m.group(1))
        if "duckduckgo.com" in url:
            continue
        title = _html.unescape(_re.sub("<[^>]+>", "", m.group(2))).strip()
        snippet = _html.unescape(_re.sub("<[^>]+>", " ", m.group(3)))
        snippet = " ".join(snippet.split())[:300]
        results.append((title, url, snippet))

    if not results:
        # Различаем «вопрос пуст» и «парсер сломался»: если DDG вернул страницу
        # в неожиданной вёрстке (нет result-link), молчать нельзя — иначе
        # хрупкий regex превращается в тихий отказ поиска.
        if "result-link" not in body:
            return (
                f"Поиск не разобрал страницу DDG (вёрстка изменилась?) — "
                f"результаты не извлечены. Это СБОЙ парсера, а не «ничего нет»."
            )
        return f"По запросу «{query}» ничего не нашлось."
    out = [f"Результаты поиска: «{query}»"]
    for i, (title, url, snippet) in enumerate(results, 1):
        out.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n".join(out)


def web_fetch(url: str, max_chars: int = 6000) -> str:
    """Забирает страницу по URL и возвращает чистый текст (без тегов)."""
    url = (url or "").strip()
    if not _re.match(r"^https?://", url):
        return _err("нужен полный URL, начинающийся с http:// или https://")
    try:
        max_chars = max(500, min(int(max_chars), 30_000))
    except (TypeError, ValueError):
        max_chars = 6000
    req = urllib.request.Request(url, headers=_UA)
    # Ответ читаем с потолком: гигабайтная страница не должна съесть RAM сервера.
    max_fetch_bytes = 4_000_000
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read(max_fetch_bytes + 1)
    except Exception as e:
        return f"Не открыл {url}: {type(e).__name__}: {e}"
    fetch_truncated = len(raw) > max_fetch_bytes
    if fetch_truncated:
        raw = raw[:max_fetch_bytes]

    text = raw.decode("utf-8", "replace")
    # Выкидываем скрипты/стили, собираем текст
    text = _re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<!--.*?-->", " ", text, flags=_re.DOTALL)
    text = _re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    text = _re.sub(r"\n{3,}", "\n\n", text).strip()

    if not text:
        return f"Страница {url} пустая или только скрипты."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[текст обрезан]"
    if fetch_truncated:
        text += "\n...[исходная страница больше лимита чтения — обрезана по байтам]"
    return f"{url}\n{text}"


# ---------- напоминания/календарь (всегда доступны) ----------

REMINDERS_DB = HOME / "myagent" / "reminders.db"

_DAYS_RU = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _rem_conn():
    conn = sqlite3.connect(REMINDERS_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER,
                        text TEXT NOT NULL,
                        fire_at TEXT NOT NULL,
                        created TEXT DEFAULT CURRENT_TIMESTAMP
                    )''')
    conn.commit()
    return conn


def _parse_when(when: str, now=None) -> str | None:
    """
    Парсит текстовое время в ISO 'YYYY-MM-DD HH:MM'.
    Поддерживает: «через N сек/мин/час/день/дней», «сегодня/завтра HH:MM»,
    «пн-вс HH:MM», «HH:MM», «YYYY-MM-DD HH:MM».
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    # Время ЕДИНОЕ (московское): парсер и планировщик смотрят на одни часы.
    now = now or datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    w = (when or "").strip().lower().replace(".", ":").replace(",", " ")
    w = " ".join(w.split())
    if not w:
        return None

    # «через 10 минут» / «через минуту» / «через 5 сек» / «через 2 часа» / «через 3 дня»
    if w.endswith("полчаса") or "через полчаса" in w:
        w = w.replace("через полчаса", "через 30 минут").replace("полчаса", "30 минут")
    word_num = {"одну": 1, "один": 1, "два": 2, "две": 2, "три": 3}
    m = _re.match(r"^(?:через|спустя)\s+(?:(\d+|одну|один|два|две|три)\s+)?"
                  r"(сек(?:унд(?:[уаы])?)?|мин(?:ут(?:[уаы])?)?|час(?:а|ов)?|дн(?:ья|ей|я)?|день)\s*$", w)
    if m:
        num = m.group(1)
        if num:
            n = word_num.get(num, None)
            n = int(num) if n is None else n
        else:
            n = 1  # «через минуту» / «через час» без числа
        unit = m.group(2)
        if unit.startswith("сек"):
            delta = timedelta(seconds=n)
        elif unit.startswith("мин"):
            delta = timedelta(minutes=n)
        elif unit.startswith("час"):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return (now + delta).strftime("%Y-%m-%d %H:%M")

    # ISO: 2026-09-27 09:00 | 2026-09-27T09:00
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(w.replace("t", " "), fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

    # сегодня/завтра/послезавтра HH:MM
    m = _re.match(r"^(сегодня|завтра|послезавтра|после завтра)\s+(\d{1,2})[:.](\d{2})$", w)
    if m:
        day_off = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "после завтра": 2}[m.group(1)]
        base = now + timedelta(days=day_off)
        return base.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    # день недели HH:MM
    m = _re.match(r"^([а-яё]{2,3})\s+(\d{1,2})[:.](\d{2})$", w)
    if m and m.group(1) in _DAYS_RU:
        target = _DAYS_RU[m.group(1)]
        days_ahead = (target - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        base = now + timedelta(days=days_ahead)
        return base.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    # просто HH:MM — сегодня, а если уже прошло, то завтра
    m = _re.match(r"^(\d{1,2})[:.](\d{2})$", w)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand.strftime("%Y-%m-%d %H:%M")

    return None


def reminder_add(text: str, when: str, chat_id: int = None) -> str:
    """Ставит напоминание. Возвращает id и распознанное время."""
    text = (text or "").strip()
    if not text:
        return _err("пустой текст напоминания")
    iso = _parse_when(when)
    if not iso:
        return _err(
            f"не распознал время «{when}». Поддержка: «через 10 минут», "
            "«сегодня 21:00», «завтра 09:30», «пн 10:00», «2026-09-27 09:00», «21:00»."
        )
    conn = _rem_conn()
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, text, fire_at) VALUES (?, ?, ?)",
        (int(chat_id) if chat_id else None, text, iso),
    )
    conn.commit()
    conn.close()
    return f"OK: напоминание #{cur.lastrowid} на {iso}: {text}"


def reminder_list() -> str:
    conn = _rem_conn()
    rows = conn.execute(
        "SELECT id, fire_at, text FROM reminders ORDER BY fire_at"
    ).fetchall()
    conn.close()
    if not rows:
        return "Напоминаний нет."
    return "\n".join(f"#{rid} [{fire_at}] {text}" for rid, fire_at, text in rows)


def reminder_delete(reminder_id: int) -> str:
    conn = _rem_conn()
    cur = conn.execute("DELETE FROM reminders WHERE id=?", (int(reminder_id),))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return _err(f"напоминания #{reminder_id} нет.")
    return f"OK: удалил напоминание #{reminder_id}."


def reminders_due(now_iso: str) -> list[tuple[int, int, str]]:
    """(id, chat_id, text) для напоминаний, у которых время пришло."""
    conn = _rem_conn()
    rows = conn.execute(
        "SELECT id, chat_id, text FROM reminders WHERE fire_at <= ?", (now_iso,)
    ).fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


def reminders_forget(reminder_id: int):
    conn = _rem_conn()
    conn.execute("DELETE FROM reminders WHERE id=?", (int(reminder_id),))
    conn.commit()
    conn.close()


# ---------- голос: синтез речи (TTS) ----------
#
# Внутренняя функция, не инструмент модели: бот озвучивает мои ответы,
# когда у Саши включён тумблер «Голос».

import uuid as _uuid


def speech_synth(text: str, voice: str = "Sadachbia") -> str:
    """
    Синтезирует речь, возвращает путь к wav (или ОТКАЗ).

    Важно: gemini-3.8-flash-lite-tts умеет ТОЛЬКО response_format=pcm
    (raw PCM 24kHz mono, little-endian, 16-bit) — mp3 API отдаёт с 400.
    Поэтому оборачиваем PCM в WAV-контейнер руками: Telegram mp3 не обязан
    принимать, а wav — стандарт и играет везде.
    """
    text = (text or "").strip()
    if not text:
        return _err("пустой текст для озвучки")
    text = text[:4000]

    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(HOME / "myagent" / ".env")
    key = os.getenv("AITUNNEL_API_KEY")
    if not key:
        return _err("нет AITUNNEL_API_KEY в .env")

    client = OpenAI(api_key=key, base_url="https://api.aitunnel.ru/v1/")
    out_dir = HOME / "myagent" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"speech_{_uuid.uuid4().hex}.wav"
    try:
        response = client.audio.speech.create(
            model="gemini-3.8-flash-lite-tts",
            voice=voice,
            input=text,
            response_format="pcm",
        )
        pcm = response.content
    except Exception as e:
        return _err(f"TTS не сработал: {type(e).__name__}: {e}")

    if not pcm or all(b == 0 for b in pcm[:1000]):
        return _err("TTS вернул пустые данные")

    # Оборачиваем raw PCM (24000 Hz, 1 ch, 16-bit LE) в WAV
    rate, channels, bits = 24000, 1, 16
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")          # PCM
        + channels.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits.to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    try:
        path.write_bytes(header + pcm)
    except Exception as e:
        return _err(f"не записал wav: {e}")
    return str(path)


_FFMPEG = str(HOME / "myagent" / "venv" / "lib" / "python3.14" / "site-packages" /
              "imageio_ffmpeg" / "binaries" / "ffmpeg-linux-x86_64-v7.0.2")


def voice_synth(text: str, voice: str = "Sadachbia") -> str:
    """
    Полный цикл голосового: синтез → wav → ogg/opus → путь к ogg (или ОТКАЗ).
    ogg/opus — формат Telegram для голосовых с кнопкой микрофона (send_voice).
    """
    wav_path = speech_synth(text, voice)
    if isinstance(wav_path, str) and wav_path.startswith("ОТКАЗ"):
        return wav_path

    ogg_path = wav_path.rsplit(".", 1)[0] + ".ogg"
    import subprocess
    try:
        proc = subprocess.run(
            [_FFMPEG, "-y", "-i", wav_path, "-c:a", "libopus",
             "-b:a", "48k", "-ar", "48000", "-ac", "1", ogg_path],
            capture_output=True, timeout=60,
        )
    except Exception as e:
        os.remove(wav_path)
        return _err(f"ffmpeg не запустился: {e}")
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass

    if proc.returncode != 0 or not os.path.exists(ogg_path):
        err = (proc.stderr or b"").decode(errors="replace")[-300:]
        return _err(f"конвертация в ogg не удалась: {err}")
    return ogg_path


# ---------- схема для API ----------

TOOLS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Читает файл с номерами строк. Возвращает общее число строк. Безопаснее, чем cat: видно позиции.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Абсолютный или относительный от /home/sasha путь"},
                    "start_line": {"type": "integer", "description": "Первая строка (1-based), по умолчанию 1"},
                    "end_line": {"type": "integer", "description": "Последняя строка, по умолчанию конец файла"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Точечная замена в файле. old_text должен встречаться РОВНО один раз, иначе отказ и файл не тронут. Перед записью создаёт бэкап. Возвращает diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "description": "Фрагмент для поиска, включая отступы"},
                    "new_text": {"type": "string", "description": "На что заменить"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Создаёт новый файл. По умолчанию create_only=true и на существующий файл не пишет. create_only=false перезапишет, но с бэкапом и diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_only": {"type": "boolean", "description": "true по умолчанию: писать только если файла нет"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Поиск по коду с file:line. Сам исключает мусор (venv, backups, __pycache__, .bak, .log). Точнее и чище, чем grep в bash.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Где искать, по умолчанию /home/sasha"},
                    "glob": {"type": "string", "description": "Маска имени файла, например *.py"},
                    "fixed": {"type": "boolean", "description": "true = точная подстрока без regex"},
                    "context": {"type": "integer", "description": "Сколько строк вокруг совпадения, 0-5"},
                    "ignore_junk": {"type": "boolean", "description": "true по умолчанию"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Сохраняет факт в долговременную память (всегда доступна, даже без терминала). Клади сюда важное: факты о людях, договорённости, решения. Дедупликация автоматическая.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Кому принадлежит факт (id собеседника)"},
                    "fact": {"type": "string", "description": "Суть факта, до 2000 символов"},
                },
                "required": ["user_id", "fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_load",
            "description": "Читает долговременную память по user_id (всегда доступна). Новые факты в конце списка. Используй в начале разговора, чтобы вспомнить контекст.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Чью память читать"},
                    "limit": {"type": "integer", "description": "Сколько фактов вернуть, 1-50, по умолчанию 20"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": "Удаляет факт из памяти. Нужен текст факта точь-в-точь (можно взять из memory_load).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Чью память чистить"},
                    "fact": {"type": "string", "description": "Текст факта как в памяти"},
                },
                "required": ["user_id", "fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете через DuckDuckGo. Возвращает заголовки, ссылки и описания. Используй для свежих фактов, новостей, уточнений.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "limit": {"type": "integer", "description": "Сколько результатов, 1-8, по умолчанию 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Открывает страницу по URL и возвращает чистый текст без тегов. Нужен полный URL с http/https.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Полный URL страницы"},
                    "max_chars": {"type": "integer", "description": "Сколько символов вернуть, 500-30000, по умолчанию 6000"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_add",
            "description": "Ставит напоминание на время. Время текстом: «через 10 минут», «сегодня 21:00», «завтра 09:30», «пн 10:00», «2026-09-27 09:00», «21:00».",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Что напомнить"},
                    "when": {"type": "string", "description": "Когда сработать (текстовое время)"},
                    "chat_id": {"type": "integer", "description": "Кому доставить, по умолчанию владелец"},
                },
                "required": ["text", "when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_list",
            "description": "Показывает все запланированные напоминания.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_delete",
            "description": "Удаляет напоминание по его id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "description": "id напоминания"},
                },
                "required": ["reminder_id"],
            },
        },
    },
]

_RAW_DISPATCH = {
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "search_code": search_code,
    "memory_save": memory_save,
    "memory_load": memory_load,
    "memory_forget": memory_forget,
    "web_search": web_search,
    "web_fetch": web_fetch,
    "reminder_add": reminder_add,
    "reminder_list": reminder_list,
    "reminder_delete": reminder_delete,
}


def _capped(fn):
    def wrapper(**kwargs):
        out = fn(**kwargs)
        if len(out) > MAX_TOOL_OUTPUT:
            out = out[:MAX_TOOL_OUTPUT] + "\n...[вывод обрезан, уточни параметры запроса]"
        return out
    return wrapper


DISPATCH = {name: _capped(fn) for name, fn in _RAW_DISPATCH.items()}

# Память работает всегда (не зависит от тумблера терминала)
MEMORY_TOOL_NAMES = {"memory_save", "memory_load", "memory_forget"}

# «Мозги» и связь с миром: память, интернет, напоминания — всегда доступны,
# это не серверные команды, а функции самого агента.
ALWAYS_TOOL_NAMES = MEMORY_TOOL_NAMES | {"web_search", "web_fetch", "reminder_add", "reminder_list", "reminder_delete"}
