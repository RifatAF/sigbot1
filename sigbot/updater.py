"""Самообновление без ручного перезапуска.

`python -m sigbot run` запускает маленький сторож, а сам бот работает в дочернем процессе в том же окне.
Бот раз в 2 минуты проверяет GitHub. Нашёл новые изменения: пишет владельцу и выходит с кодом 3.
Сторож делает git pull, при изменении requirements.txt ставит зависимости и сразу запускает бота заново.
Работает и из VS Code (F5), и из run_sigbot.bat. Выключить: AUTO_UPDATE=0 в .env."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

UPDATE_EXIT = 3


def _git(*a, timeout=120):
    return subprocess.run(["git", *a], capture_output=True, text=True, timeout=timeout)


def _hash(p: str) -> str:
    f = Path(p)
    return hashlib.sha1(f.read_bytes()).hexdigest() if f.exists() else ""


def pending() -> list[str]:
    """Заголовки новых коммитов на GitHub, которых ещё нет локально (пусто, если обновлений нет)."""
    if _git("fetch", "--quiet").returncode != 0:
        return []
    r = _git("log", "--format=%s", "HEAD..@{u}")
    return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []


def apply() -> tuple[bool, str]:
    old = _git("rev-parse", "HEAD").stdout.strip()
    req = _hash("requirements.txt")
    r = _git("pull", "--ff-only")
    if r.returncode != 0:
        return False, ("не смог скачать обновление: в папке бота есть изменённые файлы или нет связи с GitHub. "
                       + (r.stderr or r.stdout).strip()[:200])
    if _hash("requirements.txt") != req:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], timeout=900)
    subj = _git("log", "--format=%s", f"{old}..HEAD").stdout.splitlines()
    return True, "; ".join(s[:160] for s in subj[:3]) or "без описания"


def supervise() -> int:
    env = dict(os.environ, SIGBOT_CHILD="1")
    cmd = [sys.executable, "-m", "sigbot", *sys.argv[1:]]
    while True:
        try:
            rc = subprocess.call(cmd, env=env)
        except KeyboardInterrupt:
            return 0
        if rc != UPDATE_EXIT:
            return rc
        print("\n🔄 Ставлю обновление…", flush=True)
        ok, msg = apply()
        env["SIGBOT_UPDATED"] = ("✅ " if ok else "⚠️ ") + msg
        print(env["SIGBOT_UPDATED"] + "\nПерезапускаю бота.\n", flush=True)
        time.sleep(2)
