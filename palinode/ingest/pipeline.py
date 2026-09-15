"""
Palinode Ingestion Pipeline

Processes files dropped into inbox/raw/ or passed directly to endpoints:
  PDF → extract text → summarize → write to research/
  Audio → Transcriptor API → transcript → write to research/
  URL (.url/.webloc/text) → fetch → readability → write to research/
  Markdown/text → classify → file into appropriate bucket

Each ingested document produces a research reference file with provenance.
"""
from __future__ import annotations

import os
import re
import time
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
import urllib.parse
import urllib.request
import socket
import ipaddress

from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.hashing import stable_md5_hexdigest

logger = logging.getLogger("palinode.ingest")

# Redirect statuses whose Location the fetcher follows itself, one vetted
# hop at a time, and the cap on how many it will follow.
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_REDIRECT_HOPS = 5


def _is_fetchable_address(addr: str) -> bool:
    """True only for an address the ingester is allowed to connect to.

    Policy rather than enumeration: ``is_global`` is false for private,
    loopback, link-local, carrier-grade NAT (100.64.0.0/10), reserved,
    unspecified, and documentation ranges, in both IPv4 and IPv6. Multicast is
    the one class ``ipaddress`` still reports as global, so it is excluded
    explicitly.
    """
    try:
        # A scoped IPv6 literal (``fe80::1%en0``) carries a zone id that
        # ``ip_address`` rejects; the address itself is what we vet.
        ip = ipaddress.ip_address(addr.partition("%")[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def _vetted_addresses(url: str) -> list[str] | None:
    """Every address *url*'s host resolves to, or ``None`` if it may not be fetched.

    The host is resolved with ``getaddrinfo``, so IPv6-only hosts resolve (and
    IPv6 non-global literals are rejected on policy rather than by accident of
    an IPv4-only lookup). *Every* answer must be fetchable: a host that returns
    one public and one internal address is refused outright, since which one a
    later connect picks is not ours to choose.

    The addresses are returned rather than discarded so the connection can be
    made to one of them. Resolving a name, approving it, and then handing the
    *name* to the HTTP client is check-then-use: the client resolves again, and
    the second answer is not the one that was vetted.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None

        hostname = parsed.hostname
        if not hostname:
            return None

        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except (OSError, ValueError, UnicodeError):
            return None

        # De-duplicated, in the order the resolver returned them.
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses or not all(_is_fetchable_address(a) for a in addresses):
            return None
        return addresses
    except Exception:
        return None


def is_safe_url(url: str) -> bool:
    """Validates URL for SSRF protection.

    A pre-flight check for callers that want to refuse a URL before starting an
    ingest. The authoritative check is the one :func:`_fetch_vetted_url` runs
    per hop, because only that one pins the connection to what it approved.
    """
    return _vetted_addresses(url) is not None


def _tunnels_through_http_proxy(parsed: urllib.parse.ParseResult) -> bool:
    """True when an https request for *parsed* would ride an HTTP CONNECT tunnel.

    ``httpcore`` hardcodes the TLS ``server_hostname`` of a tunnelled
    connection to the CONNECT target and ignores the ``sni_hostname``
    extension, so a pinned request through one would have its certificate
    verified against the IP literal instead of the hostname. Rather than
    weaken verification, pinning is skipped for that case (see the warning in
    :func:`_fetch_vetted_url`). SOCKS proxies honour the extension and are
    unaffected.
    """
    if parsed.scheme != "https":
        return False
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if not proxy or proxy.partition("://")[0].lower().startswith("socks"):
        return False
    try:
        return not urllib.request.proxy_bypass(parsed.hostname or "")
    except (OSError, ValueError):
        return True


def _get_pinned(
    client: httpx.Client, parsed: urllib.parse.ParseResult, addresses: list[str]
) -> httpx.Response:
    """GET *parsed* over a connection to one of its already-vetted *addresses*.

    The request URL carries the vetted address as a literal, so nothing
    re-resolves the name between the check and the connect. ``Host`` and the
    TLS ``sni_hostname`` extension keep the request — and certificate
    verification — pointed at the original hostname. Addresses are tried in
    resolution order, so a host whose first record is unreachable is still
    reached; unlike the socket layer's own iteration, which shared one deadline
    across the set, the timeout now applies per attempt.
    """
    credentials, at, authority = parsed.netloc.rpartition("@")
    last_error: Exception | None = None
    for address in addresses:
        literal = f"[{address}]" if ":" in address else address
        netloc = literal if parsed.port is None else f"{literal}:{parsed.port}"
        pinned = urllib.parse.urlunparse(
            parsed._replace(netloc=f"{credentials}{at}{netloc}")
        )
        try:
            return client.get(
                pinned,
                headers={"Host": authority},
                extensions={"sni_hostname": parsed.hostname},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Only a failure to establish the connection falls through to the
            # next address; past that point the request has already been sent.
            last_error = e
    raise last_error  # type: ignore[misc]  # addresses is never empty


def _fetch_vetted_url(url: str) -> httpx.Response | None:
    """GET *url*, vetting the address behind every redirect hop and connecting to it.

    ``httpx`` is told not to follow redirects: a guard that runs once on the
    submitted URL says nothing about where a ``Location`` header points. Each
    hop is resolved and vetted before it is requested, and the chain is bounded
    so a server cannot walk the fetcher through an unbounded list of targets.

    Each hop then connects to the address it vetted, not to the name, so a host
    whose second answer differs from its first — DNS rebinding — cannot be
    reached through the window between the two.

    Returns the final response, or ``None`` when a hop is refused (nothing is
    fetched from it and the caller writes nothing).
    """
    current = url
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            addresses = _vetted_addresses(current)
            if addresses is None:
                logger.error(f"URL fetch blocked by SSRF protection: {current}")
                return None

            parsed = urllib.parse.urlparse(current)
            if _tunnels_through_http_proxy(parsed):
                logger.warning(
                    "Connecting by name through an HTTPS proxy, which cannot carry a "
                    f"pinned address without breaking certificate verification: {current}"
                )
                response = client.get(current)
            else:
                response = _get_pinned(client, parsed, addresses)

            if response.status_code not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("location", "")
            if not location:
                logger.error(f"Redirect without a location header: {current}")
                return None
            # Relative targets resolve against the hop we are on, then get
            # vetted like any other.
            current = urllib.parse.urljoin(current, location)

    logger.error(f"Too many redirects while fetching: {url}")
    return None


def process_inbox() -> None:
    """Scan ingestion directories for new files."""
    raw_dir = os.path.join(config.palinode_dir, config.ingestion.inbox_dir)
    processed_dir = os.path.join(config.palinode_dir, config.ingestion.processed_dir)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    for filename in os.listdir(raw_dir):
        filepath = os.path.join(raw_dir, filename)
        if not os.path.isfile(filepath):
            continue

        logger.info(f"Processing: {filename}")
        try:
            result = process_file(filepath)
            if result:
                dest = os.path.join(processed_dir, filename)
                os.rename(filepath, dest)
                logger.info(f"Done: {filename} → {result}")
            else:
                logger.warning(f"No result for: {filename}")
        except Exception as e:
            logger.error(f"Failed to process {filename}: {e}")


def process_file(filepath: str) -> str | None:
    """Invoke parser based on file extension.

    Args:
        filepath (str): Path to the input file.

    Returns:
        str | None: Returns absolute path of the saved file if successful, or None.
    """
    ext = Path(filepath).suffix.lower()
    name = Path(filepath).stem

    if ext in (".pdf",):
        return ingest_pdf(filepath, name)
    elif ext in (".m4a", ".mp3", ".wav", ".ogg", ".flac"):
        return ingest_audio(filepath, name)
    elif ext in (".mp4", ".mkv", ".mov", ".webm"):
        return ingest_audio(filepath, name) # Extract audio track
    elif ext in (".md", ".txt"):
        return ingest_text(filepath, name)
    elif ext in (".url", ".webloc"):
        return ingest_url_file(filepath, name)
    else:
        logger.warning(f"Unknown file type: {ext}")
        return None


def ingest_pdf(filepath: str, name: str) -> str | None:
    """Extract text blocks from unparsed PDF layouts formatted as Markdown.

    Args:
        filepath (str): Path to input PDF.
        name (str): Document basename.

    Returns:
        str | None: Destination saved file path if completed successfully.
    """
    try:
        try:
            import fitz  # pymupdf
            doc = fitz.open(filepath)
            text = "\n\n".join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            result = subprocess.run(
                ["pdftotext", filepath, "-"],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
            text = result.stdout

        if not text.strip():
            logger.warning(f"Empty PDF: {filepath}")
            return None

        # Cap for very large PDFs
        capped_len = config.ingestion.pdf_max_chars
        
        return write_research_file(
            name=name,
            content=text[:capped_len],
            source_file=os.path.basename(filepath),
            file_type="pdf",
        )
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return None


def ingest_audio(filepath: str, name: str) -> str | None:
    """Send audio file to the remote transcription service.

    Args:
        filepath (str): Path to the media file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    url = config.ingestion.transcriptor.url
    timeout_sec = config.ingestion.transcriptor.timeout_seconds
    
    try:
        with open(filepath, "rb") as f:
            response = httpx.post(
                f"{url}/transcribe",
                files={"file": (os.path.basename(filepath), f)},
                timeout=httpx.Timeout(float(timeout_sec), connect=10.0),
            )
            response.raise_for_status()
            data = response.json()

        text = data.get("text", "")
        if not text:
            logger.warning(f"Empty transcript: {filepath}")
            return None

        return write_research_file(
            name=name,
            content=text,
            source_file=os.path.basename(filepath),
            file_type="audio_transcript",
        )
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return None


def ingest_text(filepath: str, name: str) -> str | None:
    """Ingest a text file, delegating to URL ingestion if content is a URL.

    Args:
        filepath (str): Path to the input file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    stripped = content.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return ingest_url(stripped, name)

    return write_research_file(
        name=name,
        content=content,
        source_file=os.path.basename(filepath),
        file_type="text",
    )


def ingest_url_file(filepath: str, name: str) -> str | None:
    """Read macOS .webloc or Windows .url shortcut files.

    Args:
        filepath (str): Path to the shortcut file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # .url format
    url_match = re.search(r"URL=(.+)", content)
    if url_match:
        return ingest_url(url_match.group(1).strip(), name)

    # .webloc is XML plist
    url_match = re.search(r"<string>(https?://[^<]+)</string>", content)
    if url_match:
        return ingest_url(url_match.group(1).strip(), name)

    logger.warning(f"Could not extract URL from: {filepath}")
    return None


def ingest_url(url: str, name: str) -> str | None:
    """Fetch web page content and clean main text using readability.

    Args:
        url (str): HTTP or HTTPS URL to fetch and convert into a research file.
        name (str): Fallback document slug.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    try:
        response = _fetch_vetted_url(url)
        if response is None:
            return None
        response.raise_for_status()
        html = response.text

        # Simple readability: strip HTML tags
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 100:
            logger.warning(f"Too little content from URL: {url}")
            return None

        capped_len = config.ingestion.url_max_chars
        return write_research_file(
            name=name,
            content=text[:capped_len],
            source_url=url,
            file_type="url",
        )
    except Exception as e:
        logger.error(f"URL fetch failed for {url}: {e}")
        return None


def write_research_file(
    name: str,
    content: str,
    source_file: str = "",
    source_url: str = "",
    file_type: str = "text",
) -> str:
    """Write research Markdown file with YAML frontmatter.

    Args:
        name (str): Title used to derive the research file name and heading.
        content (str): Text written to the body of the research file.
        source_file (str): Path to the source file.
        source_url (str): Optional URL from which the research content was fetched.
        file_type (str): Source file extension or type.

    Returns:
        str: Path to the created research file.
    """
    today = time.strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())[:50].strip("-")
    filename = f"{today}-{slug}.md"
    filepath = os.path.join(config.palinode_dir, "research", filename)

    if os.path.exists(filepath):
        slug += f"-{stable_md5_hexdigest(content[:100])[:6]}"
        filename = f"{today}-{slug}.md"
        filepath = os.path.join(config.palinode_dir, "research", filename)

    fm = {
        "id": f"research-{slug}",
        "category": "research",
        "source_url": source_url or "",
        "source_file": source_file or "",
        "source_type": file_type,
        "date": today,
        "tags": [],
        # timezone-aware UTC ISO-8601. Previously used
        # ``time.strftime("...Z")`` which emits local time stamped as UTC.
        "last_updated": datetime.now(UTC).isoformat(),
    }

    doc = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n# {name}\n\n{content}\n"

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    git_tools.write_memory_file(filepath, doc)

    return filepath


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    process_inbox()
