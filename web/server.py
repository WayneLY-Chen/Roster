"""啟動網頁版：挑一個空的埠、產生通行碼、打開瀏覽器。"""

from __future__ import annotations

import secrets
import socket
import threading
import webbrowser

import uvicorn

from core.config import AppConfig
from web.app import create_app

#: 只聽本機。改成 0.0.0.0 等於把整份名單開放給同一個網路裡的所有人。
HOST = "127.0.0.1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def serve(config: AppConfig, open_browser: bool = True) -> None:
    """一直跑到使用者按 Ctrl+C（或關掉終端機視窗）。"""
    token = secrets.token_urlsafe(32)
    port = _free_port()
    url = f"http://{HOST}:{port}/?token={token}"

    print(f"\n名單匠網頁版已啟動：\n  {url}\n")
    print("瀏覽器沒有自動打開的話，複製上面的網址貼到瀏覽器。")
    # flush：輸出導到檔案或管線時預設會緩衝，網址會遲遲不出現。
    print("要結束：關掉這個視窗，或按 Ctrl+C。\n", flush=True)

    if open_browser:
        # 晚一秒再開，伺服器才來得及開始聽。
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    uvicorn.run(create_app(config, token), host=HOST, port=port, log_level="warning")
