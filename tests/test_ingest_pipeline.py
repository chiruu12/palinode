"""Hermetic unit tests for ``palinode.ingest.pipeline``.

Every external seam is monkeypatched: the fetcher's ``httpx.Client`` (an
``httpx.MockTransport`` stands in, so requests are built for real but no socket
opens), ``httpx.post`` (no network), ``socket.getaddrinfo`` (no DNS),
``urllib.request.getproxies`` (the host machine's proxy settings must not pick
the code path), ``subprocess.run`` (no ``pdftotext``), and the optional ``fitz``
import (no pymupdf). The one seam left real is ``git_tools.write_memory_file``
— it is a plain atomic write guarded to ``config.memory_dir``, so pointing the
store at ``tmp_path`` is enough, exactly as the timestamp-consistency test for
this module does.

The SSRF tests below state the guarantee rather than a recipe: every address a
host resolves to must be globally routable, every redirect hop is vetted before
it is requested, and the connection goes to the address that was vetted rather
than to the name, so a second DNS answer cannot be reached.
"""
from __future__ import annotations

import ipaddress
import itertools
import os
import socket
import sys
import types
import urllib.parse

import httpx
import pytest
import yaml

from palinode.core.config import config
from palinode.core.hashing import stable_md5_hexdigest
from palinode.ingest import pipeline

PUBLIC_IP = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"
PUBLIC_URL = "https://example.com/article"
FROZEN_DAY = "2026-01-02"

# Comfortably over ingest_url's 100-char floor once the tags are stripped.
LONG_BODY = "word " * 60


# --- helpers ---


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A tmp memory store with a frozen calendar day for stable filenames."""
    # ``palinode_dir`` is a read-only alias for ``memory_dir``.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(
        pipeline, "time", types.SimpleNamespace(strftime=lambda fmt: FROZEN_DAY)
    )
    return tmp_path


def _resolver(table):
    """A ``getaddrinfo`` stand-in: IP literals resolve to themselves, names
    resolve via *table* (host → list of addresses), anything else is
    unresolvable."""

    def getaddrinfo(host, port=None, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            if host not in table:
                raise socket.gaierror(8, "nodename nor servname provided")
            addresses = table[host]
        infos = []
        for addr in addresses:
            if ":" in addr:
                infos.append(
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port or 0, 0, 0))
                )
            else:
                infos.append(
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 0))
                )
        return infos

    return getaddrinfo


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch):
    """No proxy unless a test asks for one — whether the machine running the
    tests has one configured must not decide which connection path is taken."""
    monkeypatch.setattr(pipeline.urllib.request, "getproxies", dict)


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"example.com": [PUBLIC_IP]})
    )


class FakeResponse:
    def __init__(self, text="", *, error=None, json_data=None, status_code=200, headers=None):
        self.text = text
        self._error = error
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self._error is not None:
            raise self._error
        return self

    def json(self):
        return self._json


def _split(path):
    """(frontmatter dict, body str) of a written research file."""
    raw = open(path, encoding="utf-8").read()
    assert raw.startswith("---\n")
    _, fm, body = raw.split("---\n", 2)
    # One blank line separates the closing fence from the heading.
    assert body.startswith("\n")
    return yaml.safe_load(fm), body[1:]


def _research_files(store):
    research = store / "research"
    return sorted(p.name for p in research.iterdir()) if research.exists() else []


# --- is_safe_url ---


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "https:///nohost"],
)
def test_is_safe_url_rejects_bad_scheme_or_missing_host(url, public_dns):
    assert pipeline.is_safe_url(url) is False


def test_is_safe_url_rejects_unresolvable_host(monkeypatch):
    monkeypatch.setattr(pipeline.socket, "getaddrinfo", _resolver({}))
    assert pipeline.is_safe_url("https://nowhere.invalid/") is False


@pytest.mark.parametrize(
    "resolved",
    [
        "10.0.0.5",
        "192.168.1.10",
        "172.16.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "100.64.1.1",
        "240.0.0.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
    ],
    ids=[
        "10/8", "192.168/16", "172.16/12", "loopback", "link-local", "multicast",
        "cgnat", "reserved", "unspecified",
        "v6-loopback", "v6-unique-local", "v6-link-local", "v6-multicast",
    ],
)
def test_is_safe_url_rejects_non_global_resolution(monkeypatch, resolved):
    """Only globally routable unicast addresses are fetchable — the guard is a
    policy check, so every non-global class is covered by the same rule."""
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"example.com": [resolved]})
    )
    assert pipeline.is_safe_url("https://example.com/") is False


def test_is_safe_url_rejects_ipv6_loopback_literal(monkeypatch):
    """Rejected on policy, not as a side effect of an IPv4-only lookup."""
    monkeypatch.setattr(pipeline.socket, "getaddrinfo", _resolver({}))
    assert pipeline.is_safe_url("http://[::1]/secret") is False


def test_is_safe_url_accepts_ipv6_only_host(monkeypatch):
    """A host with only an AAAA record resolves and is accepted."""
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"v6.example.com": [PUBLIC_IPV6]})
    )
    assert pipeline.is_safe_url("https://v6.example.com/x") is True


def test_is_safe_url_rejects_host_with_any_non_global_answer(monkeypatch):
    """Every answer must be fetchable: one internal address rejects the host."""
    monkeypatch.setattr(
        pipeline.socket,
        "getaddrinfo",
        _resolver({"example.com": [PUBLIC_IP, "10.0.0.5"]}),
    )
    assert pipeline.is_safe_url(PUBLIC_URL) is False


def test_is_safe_url_accepts_public_resolution(public_dns):
    assert pipeline.is_safe_url(PUBLIC_URL) is True


def test_is_safe_url_returns_false_on_garbage(monkeypatch):
    monkeypatch.setattr(
        pipeline.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not an ip", 0))],
    )
    assert pipeline.is_safe_url("https://example.com/") is False
    assert pipeline.is_safe_url(None) is False  # type: ignore[arg-type]


def test_is_safe_url_rejects_empty_resolution(monkeypatch):
    monkeypatch.setattr(pipeline.socket, "getaddrinfo", lambda *a, **k: [])
    assert pipeline.is_safe_url(PUBLIC_URL) is False


# --- ingest_url ---


def _requested_url(request):
    """The URL the fetcher meant, reassembled from what it actually sent.

    A pinned request connects to the vetted address, so ``request.url`` holds
    an IP literal and the hostname rides in ``Host`` — that pairing *is* the
    fix, so the tests read it rather than a recorded argument.
    """
    return f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"


class _NotPinned(BaseException):
    """A request went somewhere the guard had not vetted.

    Deliberately not an ``Exception``: ``ingest_url`` catches those broadly, so
    an ``AssertionError`` raised inside the transport would be logged as a
    fetch failure and look exactly like a refusal — the one thing these tests
    exist to tell apart.
    """


def _check_pinned(request):
    """The connection target is a vetted address, and TLS still verifies the name."""
    try:
        address = ipaddress.ip_address(request.url.host)
    except ValueError:
        raise _NotPinned(f"connected by name: {request.url}") from None
    if not address.is_global or address.is_multicast:
        raise _NotPinned(f"connected to {address}")
    expected = urllib.parse.urlsplit(f"//{request.headers['host']}").hostname
    if request.extensions.get("sni_hostname") != expected:
        raise _NotPinned(
            f"sni_hostname {request.extensions.get('sni_hostname')!r}, expected {expected!r}"
        )


class _Fetches:
    """What the pipeline asked ``httpx`` to do: how it built its client, and
    every request that went through it."""

    def __init__(self):
        self.client_kwargs = []
        self.requests = []

    @property
    def urls(self):
        return [_requested_url(r) for r in self.requests]


def _patch_client(monkeypatch, handler, *, pinned=True):
    """Route the pipeline's fetches through an ``httpx.MockTransport``.

    Only the transport is faked, so the handler sees real ``httpx.Request``
    objects: the pinned URL, the ``Host`` header and the ``sni_hostname``
    extension are the ones httpx would have put on the wire. No socket is
    opened and no name is resolved. Unless *pinned* is off, every request is
    checked for having gone to a vetted address rather than to a name.
    """
    fetches = _Fetches()
    real_client = httpx.Client

    def record(request):
        fetches.requests.append(request)
        if pinned:
            _check_pinned(request)
        return handler(request)

    def factory(**kwargs):
        fetches.client_kwargs.append(kwargs)
        return real_client(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(pipeline.httpx, "Client", factory)
    return fetches


def _patch_response(monkeypatch, *, status=200, text=LONG_BODY, headers=None):
    """Serve the same response to every request (a fresh one each time — a
    response body may only be read once)."""
    return _patch_client(
        monkeypatch, lambda request: httpx.Response(status, text=text, headers=headers)
    )


def _patch_hops(monkeypatch, hops):
    """Serve a ``302`` to ``hops[url]`` where one is listed, a full body
    otherwise."""

    def handler(request):
        url = _requested_url(request)
        if url in hops:
            return httpx.Response(302, headers={"location": hops[url]})
        return httpx.Response(200, text=LONG_BODY)

    return _patch_client(monkeypatch, handler)


def _spy_vetting(monkeypatch):
    """Record every URL handed to the guard, keeping the real check."""
    vetted = []
    real = pipeline._vetted_addresses

    def spy(url):
        vetted.append(url)
        return real(url)

    monkeypatch.setattr(pipeline, "_vetted_addresses", spy)
    return vetted


def test_ingest_url_blocked_writes_nothing(store, monkeypatch, caplog):
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"example.com": ["10.1.2.3"]})
    )
    fetches = _patch_response(monkeypatch)

    assert pipeline.ingest_url("https://example.com/", "n") is None
    assert fetches.requests == []
    assert _research_files(store) == []
    assert "blocked by SSRF protection" in caplog.text


def test_ingest_url_http_error_returns_none(store, public_dns, monkeypatch, caplog):
    _patch_response(monkeypatch, status=503)

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert _research_files(store) == []
    assert "URL fetch failed" in caplog.text


def test_ingest_url_strips_markup_and_collapses_whitespace(store, public_dns, monkeypatch):
    html = (
        "<html><head><style>body { color: red }</style>"
        "<script type='text/javascript'>alert('x')</script></head>"
        "<body><h1>Title</h1>\n\n  <p>first   para</p>\n"
        f"<div>{LONG_BODY}</div></body></html>"
    )
    _patch_response(monkeypatch, text=html)

    path = pipeline.ingest_url(PUBLIC_URL, "page")
    _, body = _split(path)

    assert "alert" not in body and "color" not in body and "<" not in body
    assert "Title first para word word" in body


def test_ingest_url_too_short_returns_none(store, public_dns, monkeypatch, caplog):
    _patch_response(monkeypatch, text="<p>short</p>")

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert _research_files(store) == []
    assert "Too little content" in caplog.text


def test_ingest_url_caps_content(store, public_dns, monkeypatch):
    monkeypatch.setattr(config.ingestion, "url_max_chars", 120)
    _patch_response(monkeypatch, text="x" * 500)

    _, body = _split(pipeline.ingest_url(PUBLIC_URL, "n"))
    assert body.strip().splitlines()[-1] == "x" * 120


def test_ingest_url_success_writes_frontmatter_and_vets_each_hop_itself(
    store, public_dns, monkeypatch
):
    fetches = _patch_response(monkeypatch)

    path = pipeline.ingest_url(PUBLIC_URL, "My Page")
    fm, _ = _split(path)

    assert os.path.dirname(path) == str(store / "research")
    assert fm["source_url"] == PUBLIC_URL
    assert fm["source_type"] == "url"
    assert fm["source_file"] == ""
    # Following redirects is the fetcher's own job, one vetted hop at a time —
    # httpx must not do it for us.
    assert fetches.client_kwargs == [{"timeout": 30.0, "follow_redirects": False}]
    assert fetches.urls == [PUBLIC_URL]


def test_ingest_url_connects_to_the_address_it_vetted(store, public_dns, monkeypatch):
    """The request goes to the resolved address, with the name carried in
    ``Host`` and in the TLS ``sni_hostname`` extension so the certificate is
    still verified against the hostname."""
    fetches = _patch_response(monkeypatch)

    assert pipeline.ingest_url(PUBLIC_URL, "n") is not None

    (request,) = fetches.requests
    assert str(request.url) == f"https://{PUBLIC_IP}/article"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"


def test_ingest_url_pins_ipv6_and_keeps_the_port(store, monkeypatch):
    """An IPv6 answer is bracketed in the pinned URL and a non-default port
    survives, in both the URL and the ``Host`` header."""
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"v6.example.com": [PUBLIC_IPV6]})
    )
    fetches = _patch_response(monkeypatch)

    assert pipeline.ingest_url("https://v6.example.com:8443/x", "n") is not None

    (request,) = fetches.requests
    assert str(request.url) == f"https://[{PUBLIC_IPV6}]:8443/x"
    assert request.headers["host"] == "v6.example.com:8443"
    assert request.extensions["sni_hostname"] == "v6.example.com"


def test_ingest_url_is_not_reached_by_a_rebinding_second_answer(
    store, monkeypatch
):
    """DNS rebinding: the host answers public once, then private. Because the
    connection targets the address that was vetted, the second answer is never
    resolved and never reached."""
    answers = iter([[PUBLIC_IP], ["10.0.0.7"], ["10.0.0.7"]])
    served = []

    def getaddrinfo(host, port=None, *args, **kwargs):
        served.append(host)
        return _resolver({"example.com": next(answers)})(host, port, *args, **kwargs)

    monkeypatch.setattr(pipeline.socket, "getaddrinfo", getaddrinfo)
    fetches = _patch_client(monkeypatch, lambda r: httpx.Response(200, text=LONG_BODY))

    assert pipeline.ingest_url(PUBLIC_URL, "n") is not None

    # One lookup, and the connection used its answer rather than asking again.
    assert served == ["example.com"]
    (request,) = fetches.requests
    assert request.url.host == PUBLIC_IP
    assert ipaddress.ip_address(request.url.host).is_global


def test_ingest_url_rebinding_on_a_redirect_hop_is_refused(store, monkeypatch, caplog):
    """The flip happens between vetting hop 1 and vetting hop 2: the second hop
    is vetted on its own answer, so it is refused rather than fetched."""
    answers = iter([[PUBLIC_IP], ["10.0.0.7"]])

    def getaddrinfo(host, port=None, *args, **kwargs):
        return _resolver({"example.com": next(answers)})(host, port, *args, **kwargs)

    monkeypatch.setattr(pipeline.socket, "getaddrinfo", getaddrinfo)
    fetches = _patch_hops(monkeypatch, {PUBLIC_URL: "https://example.com/step-2"})

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert fetches.urls == [PUBLIC_URL]
    assert _research_files(store) == []
    assert "blocked by SSRF protection" in caplog.text


def test_ingest_url_falls_over_to_the_next_vetted_address(store, monkeypatch):
    """A host with two vetted addresses whose first is unreachable still
    fetches, the way the socket layer's own iteration used to."""
    second = "93.184.216.35"
    monkeypatch.setattr(
        pipeline.socket,
        "getaddrinfo",
        _resolver({"example.com": [PUBLIC_IP, second]}),
    )

    def handler(request):
        if request.url.host == PUBLIC_IP:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, text=LONG_BODY)

    fetches = _patch_client(monkeypatch, handler)

    assert pipeline.ingest_url(PUBLIC_URL, "n") is not None
    assert [r.url.host for r in fetches.requests] == [PUBLIC_IP, second]


def test_ingest_url_reports_the_last_connect_failure(store, public_dns, monkeypatch):
    """When no vetted address connects, the error surfaces rather than being
    swallowed into a silent success."""

    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    _patch_client(monkeypatch, handler)

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert _research_files(store) == []


# --- pinning vs. proxies ---


@pytest.mark.parametrize(
    "url,proxies,bypass,tunnels",
    [
        ("https://example.com/x", {"https": "http://proxy:3128"}, False, True),
        ("https://example.com/x", {"all": "http://proxy:3128"}, False, True),
        ("https://example.com/x", {"https": "http://proxy:3128"}, True, False),
        ("https://example.com/x", {"all": "socks5://proxy:1080"}, False, False),
        ("https://example.com/x", {}, False, False),
        ("http://example.com/x", {"https": "http://proxy:3128"}, False, False),
    ],
    ids=[
        "https-proxy", "all-proxy", "no_proxy-bypass", "socks", "none", "plain-http",
    ],
)
def test_tunnels_through_http_proxy(monkeypatch, url, proxies, bypass, tunnels):
    """Only an https request carried by an HTTP ``CONNECT`` tunnel loses the
    pin: a SOCKS proxy honours ``sni_hostname``, and plain http has no
    certificate to verify."""
    monkeypatch.setattr(pipeline.urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(pipeline.urllib.request, "proxy_bypass", lambda host: bypass)

    parsed = urllib.parse.urlparse(url)
    assert pipeline._tunnels_through_http_proxy(parsed) is tunnels


def test_ingest_url_connects_by_name_through_an_https_proxy(
    store, public_dns, monkeypatch, caplog
):
    """The documented trade-off: httpcore verifies a tunnelled connection
    against the ``CONNECT`` target, so pinning there would check the
    certificate against an IP literal. The address vetting still runs and the
    fall back to a name-based connect is logged rather than silent."""
    monkeypatch.setattr(
        pipeline.urllib.request, "getproxies", lambda: {"https": "http://proxy:3128"}
    )
    monkeypatch.setattr(pipeline.urllib.request, "proxy_bypass", lambda host: False)
    fetches = _patch_client(
        monkeypatch, lambda r: httpx.Response(200, text=LONG_BODY), pinned=False
    )

    assert pipeline.ingest_url(PUBLIC_URL, "n") is not None

    (request,) = fetches.requests
    assert request.url.host == "example.com"
    assert "certificate verification" in caplog.text


def test_ingest_url_still_refuses_a_private_host_through_a_proxy(
    store, monkeypatch, caplog
):
    """Skipping the pin does not skip the guard."""
    monkeypatch.setattr(
        pipeline.urllib.request, "getproxies", lambda: {"https": "http://proxy:3128"}
    )
    monkeypatch.setattr(pipeline.urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(
        pipeline.socket, "getaddrinfo", _resolver({"example.com": ["10.1.2.3"]})
    )
    fetches = _patch_client(
        monkeypatch, lambda r: httpx.Response(200, text=LONG_BODY), pinned=False
    )

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert fetches.requests == []
    assert "blocked by SSRF protection" in caplog.text


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://100.64.1.1/",
    ],
    ids=["link-local", "rfc1918", "cgnat"],
)
def test_ingest_url_refuses_redirect_to_non_global_address(
    store, public_dns, monkeypatch, caplog, target
):
    """A redirect target is vetted like a submitted URL: a non-global one is
    never requested and nothing is written."""
    fetches = _patch_hops(monkeypatch, {PUBLIC_URL: target})

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert fetches.urls == [PUBLIC_URL]
    assert _research_files(store) == []
    assert "blocked by SSRF protection" in caplog.text


def test_ingest_url_follows_vetted_redirect_chain(store, public_dns, monkeypatch):
    """Every hop — including a relative one — is vetted before it is fetched,
    and each one connects to the address that vetting returned."""
    vetted = _spy_vetting(monkeypatch)
    fetches = _patch_hops(
        monkeypatch,
        {PUBLIC_URL: "/step-2", "https://example.com/step-2": "https://example.com/final"},
    )

    path = pipeline.ingest_url(PUBLIC_URL, "n")

    chain = [PUBLIC_URL, "https://example.com/step-2", "https://example.com/final"]
    assert vetted == chain
    assert fetches.urls == chain
    assert {r.url.host for r in fetches.requests} == {PUBLIC_IP}
    # The reference records the URL as submitted, not the hop it ended on.
    fm, _ = _split(path)
    assert fm["source_url"] == PUBLIC_URL


def test_ingest_url_refuses_protocol_relative_redirect_to_non_global_host(
    store, public_dns, monkeypatch
):
    """A relative Location resolves against the hop it came from and is then
    vetted, so a chain that steps onto an internal host stops there."""
    fetches = _patch_hops(monkeypatch, {PUBLIC_URL: "//10.0.0.1/internal"})

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert fetches.urls == [PUBLIC_URL]
    assert _research_files(store) == []


def test_ingest_url_refuses_endless_redirect_chain(store, public_dns, monkeypatch, caplog):
    hops = itertools.count()
    fetches = _patch_client(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": f"https://example.com/{next(hops)}"}
        ),
    )

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert len(fetches.requests) == pipeline._MAX_REDIRECT_HOPS + 1
    assert _research_files(store) == []
    assert "Too many redirects" in caplog.text


def test_ingest_url_refuses_redirect_without_a_location(store, public_dns, monkeypatch, caplog):
    _patch_response(monkeypatch, status=302, text="")

    assert pipeline.ingest_url(PUBLIC_URL, "n") is None
    assert _research_files(store) == []
    assert "Redirect without a location header" in caplog.text


# --- ingest_pdf ---


class _Doc:
    """Just enough of a pymupdf document: iterable pages, ``close()``."""

    def __init__(self, pages):
        self._pages = [types.SimpleNamespace(get_text=lambda t=t: t) for t in pages]
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


def _fake_fitz(monkeypatch, pages=None, error=None):
    mod = types.ModuleType("fitz")
    opened = []

    def open_(path):
        if error is not None:
            raise error
        doc = _Doc(pages)
        opened.append(doc)
        return doc

    mod.open = open_
    monkeypatch.setitem(sys.modules, "fitz", mod)
    return opened


def test_ingest_pdf_joins_pages_with_fitz(store, monkeypatch):
    opened = _fake_fitz(monkeypatch, pages=["page one", "page two"])

    path = pipeline.ingest_pdf("/in/Paper (final).pdf", "Paper (final)")
    fm, body = _split(path)

    assert body == "# Paper (final)\n\npage one\n\npage two\n"
    assert fm["source_type"] == "pdf"
    assert fm["source_file"] == "Paper (final).pdf"
    assert fm["source_url"] == ""
    assert opened[0].closed


def test_ingest_pdf_empty_text_returns_none(store, monkeypatch, caplog):
    _fake_fitz(monkeypatch, pages=["  ", "\n"])

    assert pipeline.ingest_pdf("/in/blank.pdf", "blank") is None
    assert _research_files(store) == []
    assert "Empty PDF" in caplog.text


def test_ingest_pdf_caps_text(store, monkeypatch):
    monkeypatch.setattr(config.ingestion, "pdf_max_chars", 7)
    _fake_fitz(monkeypatch, pages=["abcdefghijklmnop"])

    _, body = _split(pipeline.ingest_pdf("/in/big.pdf", "big"))
    assert body == "# big\n\nabcdefg\n"


def test_ingest_pdf_falls_back_to_pdftotext(store, monkeypatch):
    # ``None`` in sys.modules makes ``import fitz`` raise ImportError.
    monkeypatch.setitem(sys.modules, "fitz", None)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(stdout="from pdftotext")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    path = pipeline.ingest_pdf("/in/doc.pdf", "doc")
    _, body = _split(path)

    assert calls[0][0] == ["pdftotext", "/in/doc.pdf", "-"]
    assert calls[0][1]["capture_output"] is True and calls[0][1]["text"] is True
    assert body == "# doc\n\nfrom pdftotext\n"


def test_ingest_pdf_extraction_error_returns_none(store, monkeypatch, caplog):
    _fake_fitz(monkeypatch, error=RuntimeError("corrupt xref"))

    assert pipeline.ingest_pdf("/in/bad.pdf", "bad") is None
    assert _research_files(store) == []
    assert "PDF extraction failed: corrupt xref" in caplog.text


# --- ingest_audio ---


def _patch_post(monkeypatch, response):
    calls = []

    def fake_post(url, **kwargs):
        # Capture the filename now; the handle is closed after the call.
        kwargs["files"] = {k: v[0] for k, v in kwargs["files"].items()}
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(pipeline.httpx, "post", fake_post)
    return calls


def test_ingest_audio_posts_to_transcriptor(store, tmp_path, monkeypatch):
    monkeypatch.setattr(config.ingestion.transcriptor, "url", "http://tx.test:1234")
    monkeypatch.setattr(config.ingestion.transcriptor, "timeout_seconds", 42)
    media = tmp_path / "talk.m4a"
    media.write_bytes(b"\x00audio")
    calls = _patch_post(monkeypatch, FakeResponse(json_data={"text": "hello there"}))

    path = pipeline.ingest_audio(str(media), "talk")
    fm, body = _split(path)

    url, kwargs = calls[0]
    assert url == "http://tx.test:1234/transcribe"
    assert kwargs["files"] == {"file": "talk.m4a"}
    assert isinstance(kwargs["timeout"], httpx.Timeout)
    assert kwargs["timeout"].read == 42.0 and kwargs["timeout"].connect == 10.0
    assert fm["source_type"] == "audio_transcript"
    assert fm["source_file"] == "talk.m4a"
    assert body == "# talk\n\nhello there\n"


def test_ingest_audio_empty_transcript_returns_none(store, tmp_path, monkeypatch, caplog):
    media = tmp_path / "silent.wav"
    media.write_bytes(b"")
    _patch_post(monkeypatch, FakeResponse(json_data={"text": ""}))

    assert pipeline.ingest_audio(str(media), "silent") is None
    assert _research_files(store) == []
    assert "Empty transcript" in caplog.text


def test_ingest_audio_http_error_returns_none(store, tmp_path, monkeypatch, caplog):
    media = tmp_path / "x.mp3"
    media.write_bytes(b"")
    _patch_post(monkeypatch, FakeResponse(error=RuntimeError("502 bad gateway")))

    assert pipeline.ingest_audio(str(media), "x") is None
    assert _research_files(store) == []
    assert "Transcription failed: 502 bad gateway" in caplog.text


# --- ingest_text / ingest_url_file ---


def _capture_ingest_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline, "ingest_url", lambda url, name: calls.append((url, name)) or "/written"
    )
    return calls


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_ingest_text_delegates_url_content(store, tmp_path, monkeypatch, scheme):
    calls = _capture_ingest_url(monkeypatch)
    src = tmp_path / "link.txt"
    src.write_text(f"  {scheme}://example.com/x \n")

    assert pipeline.ingest_text(str(src), "link") == "/written"
    assert calls == [(f"{scheme}://example.com/x", "link")]


def test_ingest_text_writes_plain_text(store, tmp_path, monkeypatch):
    calls = _capture_ingest_url(monkeypatch)
    src = tmp_path / "notes.md"
    src.write_text("Some notes.\nSee https://example.com later.\n")

    path = pipeline.ingest_text(str(src), "notes")
    fm, body = _split(path)

    assert calls == []
    assert fm["source_type"] == "text"
    assert fm["source_file"] == "notes.md"
    assert body == "# notes\n\nSome notes.\nSee https://example.com later.\n\n"


def test_ingest_url_file_parses_windows_url(store, tmp_path, monkeypatch):
    calls = _capture_ingest_url(monkeypatch)
    src = tmp_path / "site.url"
    src.write_text("[InternetShortcut]\r\nURL=https://example.com/a \r\nIconIndex=0\r\n")

    assert pipeline.ingest_url_file(str(src), "site") == "/written"
    assert calls == [("https://example.com/a", "site")]


def test_ingest_url_file_parses_webloc_plist(store, tmp_path, monkeypatch):
    calls = _capture_ingest_url(monkeypatch)
    src = tmp_path / "site.webloc"
    src.write_text(
        '<?xml version="1.0"?>\n<plist version="1.0"><dict>\n'
        "<key>URL</key>\n<string>https://example.com/b</string>\n</dict></plist>\n"
    )

    assert pipeline.ingest_url_file(str(src), "site") == "/written"
    assert calls == [("https://example.com/b", "site")]


def test_ingest_url_file_without_url_returns_none(store, tmp_path, monkeypatch, caplog):
    calls = _capture_ingest_url(monkeypatch)
    src = tmp_path / "empty.webloc"
    src.write_text("<plist><dict><key>URL</key><string>not a url</string></dict></plist>")

    assert pipeline.ingest_url_file(str(src), "empty") is None
    assert calls == []
    assert "Could not extract URL" in caplog.text


# --- process_file routing ---

_ROUTES = [
    (".pdf", "ingest_pdf"),
    *[(ext, "ingest_audio") for ext in (".m4a", ".mp3", ".wav", ".ogg", ".flac")],
    *[(ext, "ingest_audio") for ext in (".mp4", ".mkv", ".mov", ".webm")],
    (".md", "ingest_text"),
    (".txt", "ingest_text"),
    (".url", "ingest_url_file"),
    (".webloc", "ingest_url_file"),
]


@pytest.mark.parametrize("ext,target", _ROUTES, ids=[e for e, _ in _ROUTES])
def test_process_file_routes_by_extension(monkeypatch, ext, target):
    calls = {}
    for fn in ("ingest_pdf", "ingest_audio", "ingest_text", "ingest_url_file"):
        monkeypatch.setattr(
            pipeline, fn, lambda path, name, fn=fn: calls.setdefault(fn, (path, name)) and fn
        )

    assert pipeline.process_file(f"/inbox/My.File{ext.upper()}") == target
    assert calls == {target: (f"/inbox/My.File{ext.upper()}", "My.File")}


def test_process_file_unknown_extension_returns_none(caplog):
    assert pipeline.process_file("/inbox/archive.zip") is None
    assert "Unknown file type: .zip" in caplog.text


# --- process_inbox ---


def test_process_inbox_creates_dirs_when_missing(store):
    pipeline.process_inbox()

    assert (store / "inbox" / "raw").is_dir()
    assert (store / "inbox" / "processed").is_dir()


def test_process_inbox_moves_skips_and_survives_errors(store, monkeypatch, caplog):
    raw = store / "inbox" / "raw"
    raw.mkdir(parents=True)
    for name in ("ok.txt", "none.txt", "boom.txt"):
        (raw / name).write_text(name)
    (raw / "subdir").mkdir()
    (raw / "subdir" / "nested.txt").write_text("nested")

    seen = []

    def fake_process(path):
        seen.append(os.path.basename(path))
        if path.endswith("boom.txt"):
            raise RuntimeError("kaboom")
        return "/research/x.md" if path.endswith("ok.txt") else None

    monkeypatch.setattr(pipeline, "process_file", fake_process)

    pipeline.process_inbox()

    assert sorted(seen) == ["boom.txt", "none.txt", "ok.txt"]
    assert not (raw / "ok.txt").exists()
    assert (store / "inbox" / "processed" / "ok.txt").read_text() == "ok.txt"
    assert (raw / "none.txt").exists()
    assert (raw / "boom.txt").exists()
    assert (raw / "subdir" / "nested.txt").exists()
    assert "Failed to process boom.txt: kaboom" in caplog.text
    assert "No result for: none.txt" in caplog.text


# --- write_research_file ---


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Hello, World!  Foo_bar", "hello-world-foo-bar"),
        ("--Trim me--", "trim-me"),
        ("a" * 60, "a" * 50),
        # Cap lands on a separator; the strip runs after the cap.
        ("a" * 49 + " b", "a" * 49),
    ],
    ids=["punctuation", "strip", "cap", "cap-then-strip"],
)
def test_write_research_file_slug_and_filename(store, name, slug):
    path = pipeline.write_research_file(name=name, content="c")

    assert path == str(store / "research" / f"{FROZEN_DAY}-{slug}.md")
    assert _split(path)[0]["id"] == f"research-{slug}"


def test_write_research_file_frontmatter_and_body(store):
    path = pipeline.write_research_file(
        name="Doc Title", content="line one\nline two",
        source_file="doc.pdf", source_url="https://example.com/d", file_type="pdf",
    )
    fm, body = _split(path)

    assert set(fm) == {
        "id", "category", "source_url", "source_file", "source_type", "date", "tags", "last_updated",
    }
    assert fm["id"] == "research-doc-title"
    assert fm["category"] == "research"
    assert fm["source_url"] == "https://example.com/d"
    assert fm["source_file"] == "doc.pdf"
    assert fm["source_type"] == "pdf"
    assert str(fm["date"]) == FROZEN_DAY
    assert fm["tags"] == []
    assert fm["last_updated"]
    assert body == "# Doc Title\n\nline one\nline two\n"


def test_write_research_file_collision_appends_content_hash(store):
    first = pipeline.write_research_file(name="same", content="first body")
    second = pipeline.write_research_file(name="same", content="second body " * 20)

    suffix = stable_md5_hexdigest(("second body " * 20)[:100])[:6]
    assert first == str(store / "research" / f"{FROZEN_DAY}-same.md")
    assert second == str(store / "research" / f"{FROZEN_DAY}-same-{suffix}.md")
    assert _split(second)[0]["id"] == f"research-same-{suffix}"
    assert _split(first)[1].endswith("first body\n")
