import ipaddress
import socket

import pytest


def pytest_addoption(parser):
    parser.addoption("--live-openrouter", action="store_true")
    parser.addoption("--live-inference", action="store_true")


@pytest.fixture(autouse=True)
def offline_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        if not request.config.getoption("--live-openrouter"):
            pytest.skip("requires --live-openrouter")
        return

    def check(host):
        if host in (None, "localhost", b"localhost"):
            return
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise AssertionError("External network is disabled in offline tests")

    resolve = socket.getaddrinfo

    def guarded_resolve(host, *args, **kwargs):
        check(host)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    for method in ("connect", "connect_ex", "sendto"):
        original = getattr(socket.socket, method)

        def guarded(sock, *args, original=original):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                check(args[-1][0])
            return original(sock, *args)

        monkeypatch.setattr(socket.socket, method, guarded)