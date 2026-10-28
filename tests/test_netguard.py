"""爬蟲不能被名錄上的網址帶去連使用者的內網或本機。整份測試不連網路。"""

from __future__ import annotations

import socket

import httpx
import pytest

from crawler import netguard
from crawler.fetcher import HttpxFetcher
from crawler.netguard import (
    PrivateAddressError,
    ensure_public_url,
    guard_httpx_request,
    is_public_url,
)
from crawler.robots import RobotsPolicy


@pytest.fixture(autouse=True)
def _fresh_dns_cache():
    netguard._is_public_host.cache_clear()
    yield
    netguard._is_public_host.cache_clear()


def _resolve_to(monkeypatch, address: str) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))],
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1/",
        "http://10.0.0.5/admin",
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",  # 雲端主機的 metadata 服務
    ],
)
def test_private_and_loopback_addresses_are_refused(url):
    assert not is_public_url(url)
    with pytest.raises(PrivateAddressError):
        ensure_public_url(url)


def test_a_hostname_that_resolves_to_the_lan_is_refused(monkeypatch):
    """刊登者可以把自己的網域指到 192.168.x.x，只看字面擋不住。"""
    _resolve_to(monkeypatch, "192.168.0.10")
    assert not is_public_url("https://router.attacker.example/")


def test_a_public_hostname_is_allowed(monkeypatch):
    _resolve_to(monkeypatch, "203.69.0.1")
    assert is_public_url("https://www.example.com.tw/")


def test_an_unresolvable_hostname_is_left_to_fail_on_its_own(monkeypatch):
    def fail(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert is_public_url("https://no-such-host.example/")


def test_a_redirect_into_the_lan_is_stopped_before_it_is_sent():
    """公開網站 302 轉到內網：轉址的那一跳也要被擋，不能只看第一個網址。"""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://192.168.1.1/reboot"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks={"request": [guard_httpx_request]},
    )
    with pytest.raises(PrivateAddressError):
        client.get("http://203.69.0.1/")
    assert sent == ["http://203.69.0.1/"]


def test_the_fetcher_and_robots_clients_carry_the_guard(tmp_config):
    fetcher = HttpxFetcher(tmp_config)
    robots = RobotsPolicy(user_agent="test")
    try:
        assert guard_httpx_request in fetcher._client.event_hooks["request"]
        assert guard_httpx_request in robots._http().event_hooks["request"]
    finally:
        fetcher.close()
        robots.close()
