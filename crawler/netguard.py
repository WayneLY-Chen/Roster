"""不讓爬蟲連到內網、本機或其他非公開位址。

要爬的網址幾乎都不是使用者自己打的：名錄上的「官網」欄由刊登者自己填，搜尋
結果與頁面上的連結也一樣。刊登者把官網填成 ``http://192.168.1.1``，或填一個
公開網站再 302 轉址到 ``http://127.0.0.1:8080``，程式就會替他對使用者的區網
送出請求——路由器、印表機、本機服務上那些「用 GET 就會改設定」的網址都打得到。

所以每一個請求在送出前都要過這一關，**包括轉址後的每一跳**：

* httpx：掛在 client 的 ``request`` event hook 上，轉址的每一跳都會觸發
* Playwright：掛在 browser context 的 route 上，頁面裡的圖片、腳本也一起擋

判斷方式是把主機名稱解析成 IP，只要有一個不是公開位址（``is_global``）就擋。
解析不到的不擋——連不上的網址本來就會失敗，沒有必要在這裡替它報錯。
"""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from core.constants import LogCategory
from core.errors import CrawlError
from core.logging_setup import get_logger

log = get_logger(LogCategory.CRAWL)


class PrivateAddressError(CrawlError):
    """網址指向內網、本機或其他非公開位址，不連線。"""

    def __init__(self, url: str) -> None:
        super().__init__(f"{url} 指向內網或本機，不連線")
        self.url = url


@lru_cache(maxsize=4096)
def _is_public_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass  # 不是 IP，是主機名稱，往下解析

    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return True
    # IPv6 的位址可能帶著 ``%網卡`` 的 scope，ip_address() 不收。
    addresses = {info[4][0].split("%", 1)[0] for info in infos}
    return all(ipaddress.ip_address(address).is_global for address in addresses)


def is_public_url(url: str) -> bool:
    """這個網址指向的是公開的網際網路位址嗎？"""
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and _is_public_host(host)


def ensure_public_url(url: str) -> None:
    """:raises PrivateAddressError: 網址指向內網、本機或其他非公開位址。"""
    if not is_public_url(url):
        log.warning("略過指向內網或本機的網址：{}", url)
        raise PrivateAddressError(url)


def guard_httpx_request(request: httpx.Request) -> None:
    """httpx 的 ``request`` event hook。轉址的每一跳都會經過這裡。"""
    ensure_public_url(str(request.url))
