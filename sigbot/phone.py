"""Панель на телефоне без настроек роутера: бесплатный туннель Cloudflare (cloudflared, без аккаунта).

Кнопка «📱 Панель на телефоне» в /menu запускает туннель и присылает ссылку с секретным ключом.
Без ключа по ссылке ничего не открывается. Ссылка меняется при каждом перезапуске бота, старая перестаёт работать.
Дома в одной Wi-Fi сети работает и прямая ссылка на компьютер. Бот только показывает графики: торговать через панель нельзя."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import stat
from pathlib import Path

from . import settings

log = logging.getLogger(__name__)
BIN = Path("bin")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STATE: dict = {"url": None, "proc": None}


def token(conn) -> str:
    """Ключ панели: из .env (DASHBOARD_TOKEN) или созданный один раз и сохранённый в базе."""
    t = os.environ.get("DASHBOARD_TOKEN") or settings.get(conn, "dashboard_token")
    if not t:
        t = secrets.token_urlsafe(16)
        settings.put(conn, "dashboard_token", t)
    return t


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))                 # пакет не уходит, просто выбирается сетевая карта
            return s.getsockname()[0]
    except OSError:
        return None


def _asset() -> tuple[str, str]:
    sysname, arch = platform.system().lower(), platform.machine().lower()
    a = "arm64" if arch in ("arm64", "aarch64") else "amd64"
    if sysname == "windows":
        return f"cloudflared-windows-{a}.exe", "cloudflared.exe"
    if sysname == "darwin":
        return f"cloudflared-darwin-{a}.tgz", "cloudflared"
    return f"cloudflared-linux-{a}", "cloudflared"


async def _binary(http) -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    asset, name = _asset()
    p = BIN / name
    if p.exists():
        return str(p)
    BIN.mkdir(exist_ok=True)
    data = await http.get_bytes(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}")
    if not data:
        raise RuntimeError("не скачался cloudflared с GitHub")
    if asset.endswith(".tgz"):
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            data = t.extractfile("cloudflared").read()
    p.write_bytes(data)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


async def start(http, port: int, wait: float = 40) -> str | None:
    """Запускает туннель (если ещё не запущен) и возвращает адрес https://….trycloudflare.com."""
    pr = STATE.get("proc")
    if pr and pr.returncode is None and STATE.get("url"):
        return STATE["url"]
    exe = await _binary(http)
    pr = await asyncio.create_subprocess_exec(exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    STATE.update(proc=pr, url=None)

    async def read():
        async for line in pr.stdout:
            m = URL_RE.search(line.decode(errors="replace"))
            if m and not STATE["url"]:
                STATE["url"] = m.group(0)
    asyncio.create_task(read())
    for _ in range(int(wait * 2)):
        if STATE["url"] or pr.returncode is not None:
            break
        await asyncio.sleep(0.5)
    return STATE["url"]


def stop():
    pr = STATE.get("proc")
    if pr and pr.returncode is None:
        pr.terminate()
    STATE.update(proc=None, url=None)
