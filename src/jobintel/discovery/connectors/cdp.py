"""Minimal local Chrome DevTools bridge used by browser-backed sources."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

from jobintel.discovery.connectors.base import SourceUnavailableError

DEFAULT_CDP_PORT = 9222


class CDPSession:
    """Synchronous Chrome DevTools Protocol client over a local websocket."""

    def __init__(self, port: int = DEFAULT_CDP_PORT) -> None:
        """Connect to a local Chrome browser-level debugging websocket."""
        try:
            from websocket import create_connection
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SourceUnavailableError("websocket-client is not installed") from exc
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
                payload = json.loads(response.read())
            websocket_url = str(payload["webSocketDebuggerUrl"])
            self._socket = create_connection(
                websocket_url,
                timeout=15,
                origin=f"http://127.0.0.1:{port}",
            )
        except (OSError, URLError, ValueError, KeyError) as exc:
            raise SourceUnavailableError(
                "本地浏览器桥未运行; 请先执行 jobintel setup-browser"
            ) from exc
        self._next_id = 0
        self._lock = threading.Lock()

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one CDP request and ignore unrelated asynchronous events."""
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            message: dict[str, Any] = {
                "id": message_id,
                "method": method,
                "params": params or {},
            }
            if session_id is not None:
                message["sessionId"] = session_id
            self._socket.send(json.dumps(message))
            while True:
                raw = self._socket.recv()
                response = json.loads(raw)
                if response.get("id") != message_id:
                    continue
                if "error" in response:
                    raise SourceUnavailableError(f"CDP {method} failed: {response['error']}")
                return cast(dict[str, Any], response)

    def evaluate(self, expression: str, session_id: str) -> Any:
        """Evaluate JavaScript in a page and return its serialized value."""
        response = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id,
        )
        result = response.get("result", {}).get("result", {})
        if result.get("subtype") == "error":
            raise SourceUnavailableError(
                f"browser JavaScript failed: {result.get('description', 'unknown error')}"
            )
        return result.get("value")

    def close(self) -> None:
        """Close the local websocket without touching the browser process."""
        self._socket.close()


def cdp_reachable(port: int = DEFAULT_CDP_PORT, timeout: float = 1.0) -> bool:
    """Return whether a local Chrome debugging endpoint is listening."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def setup_chrome(port: int = DEFAULT_CDP_PORT) -> dict[str, object]:
    """Launch an isolated local Chrome profile for user-controlled login."""
    profile = Path.home() / ".jobintel" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    if cdp_reachable(port):
        return {
            "ok": True,
            "already_running": True,
            "port": port,
            "profile": str(profile),
            "message": "浏览器桥已运行; 请确认专用窗口中的 BOSS 账号已登录。",
        }

    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = (
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        str(local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe"),
    )
    executable = next((value for value in candidates if Path(value).is_file()), None)
    if executable is None:
        return {
            "ok": False,
            "already_running": False,
            "port": port,
            "profile": str(profile),
            "message": "未找到 Chrome/Chromium, 请先安装浏览器。",
        }

    process = subprocess.Popen(
        [
            executable,
            f"--remote-debugging-port={port}",
            f"--remote-allow-origins=http://127.0.0.1:{port}",
            f"--user-data-dir={profile}",
            "https://www.zhipin.com/web/user/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (profile / "chrome.pid").write_text(str(process.pid), encoding="utf-8")
    return {
        "ok": True,
        "already_running": False,
        "port": port,
        "profile": str(profile),
        "message": "专用 Chrome 已启动; 请在窗口中登录 BOSS, 搜索时保持窗口运行。",
    }
