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

    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        return _err(f"файл {size} байт, лимит {MAX_READ_BYTES}. Используй start_line/end_line.")

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return _err(f"не прочитал: {exc}")

    lines = text.splitlines()
    total = len(lines)

    try:
        start = int(start_line) if start_line else 1
    except (TypeError, ValueError):
        start = 1
    try:
        end = int(end_line) if end_line else total
    except (TypeError, ValueError):
        end = total

    start = max(1, start)
    end = min(total, end)
    if start > end:
        return _err(f"start_line {start} > end_line {end}")

    # Режем по байтам, но строго по границам строк: обрубленная посередине
    # строка хуже лимита — она выглядит как конец файла.
    chunk = lines[start - 1:end]
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
    if last_shown < end:
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
]

_RAW_DISPATCH = {
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "search_code": search_code,
}


def _capped(fn):
    def wrapper(**kwargs):
        out = fn(**kwargs)
        if len(out) > MAX_TOOL_OUTPUT:
            out = out[:MAX_TOOL_OUTPUT] + "\n...[вывод обрезан, уточни параметры запроса]"
        return out
    return wrapper


DISPATCH = {name: _capped(fn) for name, fn in _RAW_DISPATCH.items()}
