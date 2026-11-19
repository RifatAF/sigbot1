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
LHR_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
STATE: dict = {"url": None, "proc": None, "log": []}


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


def lan_ips() -> list[str]:
    """Адреса компьютера в домашней сети. Через VPN основной адрес бывает «VPN-ным», поэтому берём все частные."""
    ips = []
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    ip = lan_ip()
    if ip:
        ips.append(ip)
    priv = lambda x: x.startswith("192.168.") or x.startswith("10.") or \
        (x.startswith("172.") and 16 <= int(x.split(".")[1]) <= 31)
    out = [x for x in dict.fromkeys(ips) if priv(x)]
    return sorted(out, key=lambda x: (not x.startswith("192.168."), x))[:3]


async def check(url: str, tok: str, tries: int = 12) -> tuple[bool, str]:
    """Открывается ли адрес с этого компьютера (новому адресу нужно до полуминуты). Второе значение: причина отказа."""
    import aiohttp
    why = ""
    for _ in range(tries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{url}/api/status?token={tok}", timeout=aiohttp.ClientTimeout(total=8),
                                 headers={"ngrok-skip-browser-warning": "1", "User-Agent": "sigbot-check"}) as r:
                    if r.status == 200:
                        return True, ""
                    why = f"ответ {r.status}: {(await r.text())[:80]}"
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:120]}"
        await asyncio.sleep(2.5)
    return False, why


def tunnel_errors(n: int = 4, prefix: str = "") -> list[str]:
    """Последние строки журнала туннеля: ошибки, а если их нет, просто последние строки."""
    log_ = [x for x in STATE.get("log", []) if x.startswith(prefix)] if prefix else \
        [x for x in STATE.get("log", []) if not x.startswith("lhr: ")]
    errs = [x for x in log_ if " ERR " in x or " WRN " in x or "error" in x.lower() or "fail" in x.lower()]
    alive = STATE.get("proc") and STATE["proc"].returncode is None
    out = (errs or log_)[-n:]
    if not prefix and not alive:
        out.append("cloudflared завершился")
    return out


def registered() -> bool:
    return any("Registered tunnel connection" in x for x in STATE.get("log", []))


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
    # протокол auto: сначала QUIC, при отказе сам переходит на http2
    pr = await asyncio.create_subprocess_exec(exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    STATE.update(proc=pr, url=None, log=[])

    async def read():
        async for line in pr.stdout:
            text = line.decode(errors="replace").strip()
            STATE["log"] = (STATE.get("log", []) + [text])[-60:]
            m = URL_RE.search(text)
            if m and not STATE["url"]:
                STATE["url"] = m.group(0)
    asyncio.create_task(read())
    # ждём не только адрес, но и соединение с Cloudflare: если открыть адрес раньше, компьютер и телефон
    # запоминают «такого сайта нет» на несколько минут, и ссылка не открывается, даже когда уже работает
    for _ in range(int(wait * 2)):
        if pr.returncode is not None or (STATE["url"] and any("Registered tunnel connection" in x for x in STATE["log"])):
            break
        await asyncio.sleep(0.5)
    if STATE["url"]:
        await asyncio.sleep(8)
    return STATE["url"]


async def start_lhr(port: int, wait: float = 30) -> str | None:
    """Запасной туннель localhost.run через ssh, который есть в Windows 10 и 11. Ничего не скачивает."""
    pr = STATE.get("lhr")
    if pr and pr.returncode is None and STATE.get("lhr_url"):
        return STATE["lhr_url"]
    ssh = shutil.which("ssh")
    if not ssh:
        STATE["log"] = STATE.get("log", []) + ["lhr: в Windows не установлен OpenSSH (ssh)"]
        return None
    pr = await asyncio.create_subprocess_exec(
        ssh, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull, "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes", "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    STATE.update(lhr=pr, lhr_url=None)

    async def read():
        async for line in pr.stdout:
            text = line.decode(errors="replace").strip()
            STATE["log"] = (STATE.get("log", []) + ["lhr: " + text])[-60:]
            m = LHR_RE.search(text)
            if m and not STATE["lhr_url"]:
                STATE["lhr_url"] = m.group(0)
    asyncio.create_task(read())
    for _ in range(int(wait * 2)):
        if STATE["lhr_url"] or pr.returncode is not None:
            break
        await asyncio.sleep(0.5)
    return STATE["lhr_url"]


# ---------- другие способы: всё через порт 443 или 22, которые обычно открыты, в отличие от 7844 у Cloudflare
PORTS = {"cloudflare": ("region1.v2.argotunnel.com", 7844), "pinggy": ("a.pinggy.io", 443),
         "localhost.run": ("localhost.run", 22), "ngrok": ("connect.ngrok-agent.com", 443)}
SSH = {   # провайдер: (хост, порт, аргумент -R, шаблон адреса)
    "pinggy": ("a.pinggy.io", 443, "0:127.0.0.1:{port}", re.compile(r"https://[a-z0-9-]+\.[a-z0-9.-]*pinggy\.(?:link|online)")),
    "localhost.run": ("localhost.run", 22, "80:127.0.0.1:{port}", LHR_RE),
}
NGROK_RE = re.compile(r"url=(https://[^\s]+)")


async def port_open(host: str, port: int, timeout: float = 4) -> bool:
    """Пускает ли сеть (роутер, провайдер, антивирус) исходящее соединение на этот порт."""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def diagnose() -> dict:
    res = await asyncio.gather(*(port_open(h, p) for h, p in PORTS.values()))
    return dict(zip(PORTS, res))


async def ssh_key() -> str | None:
    """Свой ключ для ssh-туннелей: без него Windows ssh не может войти без пароля («Permission denied»)."""
    key = BIN / "sigbot_ssh_key"
    if key.exists():
        return str(key)
    kg = shutil.which("ssh-keygen")
    if not kg:
        return None
    BIN.mkdir(exist_ok=True)
    pr = await asyncio.create_subprocess_exec(kg, "-t", "ed25519", "-N", "", "-q", "-f", str(key),
                                              stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                              stderr=asyncio.subprocess.DEVNULL)
    await pr.wait()
    return str(key) if key.exists() else None


async def _run(name: str, args: list[str], rx, wait: float) -> str | None:
    old = STATE.get("procs", {}).get(name)
    if old and old.returncode is None and STATE.get("urls", {}).get(name):
        return STATE["urls"][name]
    if old and old.returncode is None:
        old.terminate()
    pr = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.STDOUT)
    STATE.setdefault("procs", {})[name] = pr
    STATE.setdefault("urls", {})[name] = None

    async def read():
        async for line in pr.stdout:
            text = line.decode(errors="replace").strip()
            STATE["log"] = (STATE.get("log", []) + [f"{name}: {text}"])[-80:]
            m = rx.search(text)
            if m and not STATE["urls"][name]:
                STATE["urls"][name] = m.group(1) if rx.groups else m.group(0)
    asyncio.create_task(read())
    for _ in range(int(wait * 2)):
        if STATE["urls"][name] or pr.returncode is not None:
            break
        await asyncio.sleep(0.5)
    return STATE["urls"][name]


async def start_ssh(name: str, port: int, wait: float = 25) -> str | None:
    ssh = shutil.which("ssh")
    if not ssh:
        STATE["log"] = STATE.get("log", []) + [f"{name}: в Windows нет OpenSSH (ssh)"]
        return None
    host, hport, fwd, rx = SSH[name]
    key = await ssh_key()
    args = [ssh, "-T", "-p", str(hport), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull,
            "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15", "-R", fwd.format(port=port)]
    if key:
        args += ["-i", key, "-o", "IdentitiesOnly=yes"]
    return await _run(name, args + [host], rx, wait)


def _ngrok_asset() -> tuple[str, str]:
    sysname, arch = platform.system().lower(), platform.machine().lower()
    a = "arm64" if arch in ("arm64", "aarch64") else "amd64"
    osn = "windows" if sysname == "windows" else "darwin" if sysname == "darwin" else "linux"
    return f"https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-{osn}-{a}.zip", "ngrok.exe" if osn == "windows" else "ngrok"


async def start_ngrok(http, port: int, token: str, domain: str | None = None, wait: float = 25) -> str | None:
    """ngrok: бесплатный аккаунт даёт постоянный адрес, ссылка не меняется. Соединение через порт 443."""
    url, name = _ngrok_asset()
    exe = shutil.which("ngrok") or str(BIN / name)
    if not os.path.exists(exe):
        data = await http.get_bytes(url)
        if not data:
            raise RuntimeError("не скачался ngrok")
        import io
        import zipfile
        BIN.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            (BIN / name).write_bytes(z.read(name))
        (BIN / name).chmod((BIN / name).stat().st_mode | stat.S_IEXEC)
    args = [exe, "http", f"127.0.0.1:{port}", "--authtoken", token, "--log", "stdout", "--log-format", "logfmt"]
    if domain:
        args += ["--url", domain if domain.startswith("http") else "https://" + domain]
    return await _run("ngrok", args, NGROK_RE, wait)


def ngrok_cfg(conn) -> tuple[str | None, str | None]:
    return (os.environ.get("NGROK_AUTHTOKEN") or settings.get(conn, "ngrok_token"),
            os.environ.get("NGROK_DOMAIN") or settings.get(conn, "ngrok_domain"))


def last_lines(name: str, n: int = 2) -> str:
    x = [l[len(name) + 2:] for l in STATE.get("log", []) if l.startswith(name + ": ")]
    return " | ".join(s[-140:] for s in x[-n:]) or "нет ответа"


async def best(http, port: int, tok: str) -> tuple[str | None, str | None, list[str]]:
    """Пробует способы по очереди и проверяет каждый настоящим запросом. Возвращает (адрес, способ, отчёт)."""
    rep = []
    cur = STATE.get("best")
    if cur:                                           # уже работающий адрес: проверить и вернуть
        name, url = cur
        pr = STATE.get("proc") if name == "cloudflare" else STATE.get("procs", {}).get(name)
        if pr and pr.returncode is None and (await check(url, tok, tries=2))[0]:
            return url, name, rep
        STATE["best"] = None
    net = await diagnose()
    rep.append("Сеть: " + ", ".join(f"{k} {'✅' if v else '⛔'}" for k, v in net.items()))
    token, domain = ngrok_cfg(STATE.get("conn")) if STATE.get("conn") is not None else (None, None)
    order = []
    if token:
        order.append("ngrok")
    if net.get("cloudflare"):
        order.append("cloudflare")
    order += ["pinggy", "localhost.run"]
    for name in order:
        try:
            if name == "ngrok":
                url = await start_ngrok(http, port, token, domain)
            elif name == "cloudflare":
                url = await start(http, port)
            else:
                url = await start_ssh(name, port)
        except Exception as e:
            url, STATE["log"] = None, STATE.get("log", []) + [f"{name}: {e}"]
        if not url:
            rep.append(f"{name}: не запустился ({last_lines(name) if name != 'cloudflare' else ' | '.join(tunnel_errors(2))})")
            continue
        ok, why = await check(url, tok, tries=8)
        if ok:
            STATE["best"] = (name, url)
            rep.append(f"{name}: ✅ работает")
            return url, name, rep
        rep.append(f"{name}: адрес есть, но не открывается ({why})")
    return None, None, rep


def stop():
    for pr in (STATE.get("procs") or {}).values():
        if pr and pr.returncode is None:
            pr.terminate()
    STATE.update(procs={}, urls={}, best=None)
    pr = STATE.get("lhr")
    if pr and pr.returncode is None:
        pr.terminate()
    STATE.update(lhr=None, lhr_url=None)
    pr = STATE.get("proc")
    if pr and pr.returncode is None:
        pr.terminate()
    STATE.update(proc=None, url=None)
