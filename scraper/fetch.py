"""
Vastutab: kas me tohime seda lehte tõmmata, ja kas see on muutunud.

Ainult stdlib (urllib + html.parser) — projekt ei sõltu requests'ist ega bs4-st.

Kolm sammu enne, kui sisu töötlemiseni jõuame:
1. robots.txt luba (per domeen, vahemällu pandud)
2. viisakas paus sama domeeni järjestikuste päringute vahel
3. tingimuslik GET (If-None-Match / If-Modified-Since)

Muutuse lõplik signaal on PUHASTATUD TEKSTI sha256, mitte toore HTML-i oma:
Cloudflare vahetab e-posti obfuskeerimise tokeneid ja beacon-nonce'i iga
päringuga, nii et kaks järjestikust päringut samale lehele annavad erineva
HTML-i, aga identse puhastatud teksti. eis.ee ei tagasta ETag-i üldse.
"""

import hashlib
import html
import html.parser
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from . import config, robots


_robots_cache: dict[str, robots.Rules | None] = {}
_last_request_at: dict[str, float] = {}


class ScrapeNotAllowed(Exception):
    """robots.txt keelab selle URL-i tõmbamise."""


@dataclass
class FetchResult:
    url: str
    changed: bool
    status_code: int
    etag: str | None
    last_modified: str | None
    cleaned_text: str | None   # None kui changed=False (pole vaja uuesti töödelda)
    content_hash: str | None


def _domain(url: str) -> str:
    return urlparse(url).netloc


def http_get(url: str, headers: dict[str, str] | None = None):
    """Viisakas GET: robots-kontroll + rate limit + meie User-Agent.

    Tagastab avatud vastuse. Kutsuja vastutab lugemise eest.
    """
    if not robots_allows(url):
        raise ScrapeNotAllowed(f"robots.txt keelab: {url}")
    _respect_rate_limit(url)
    merged = {"User-Agent": config.USER_AGENT}
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    return urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_SECONDS)


def robots_allows(url: str) -> bool:
    """Kas robots.txt lubab meil seda URL-i tõmmata.

    robots.txt tõmmatakse MEIE User-Agentiga. Cloudflare vastab vaikimisi
    'Python-urllib/3.x' agendile 403-ga nii eis.ee kui riigiteataja.ee ees,
    mida robotparser tõlgendaks kui 'kõik keelatud'.
    """
    domain = _domain(url)
    if domain not in _robots_cache:
        _robots_cache[domain] = _load_robots(domain)
    rules = _robots_cache[domain]
    if rules is None:  # robots.txt puudub -> RFC järgi ei ole see keeld
        return True
    return rules.allows(url)


def _load_robots(domain: str) -> robots.Rules | None:
    """Tagastab parsitud reeglid, või None kui robots.txt puudub (= kõik lubatud)."""
    url = f"https://{domain}/robots.txt"
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        print(f"[fetch] robots.txt {domain}: HTTP {exc.code} — jätan domeeni vahele")
        raise ScrapeNotAllowed(f"robots.txt pole loetav ({domain}): HTTP {exc.code}") from exc
    except Exception as exc:
        print(f"[fetch] robots.txt {domain}: {exc} — jätan domeeni vahele")
        raise ScrapeNotAllowed(f"robots.txt pole loetav ({domain}): {exc}") from exc

    return robots.parse(body, config.USER_AGENT)


def _respect_rate_limit(url: str) -> None:
    domain = _domain(url)
    last = _last_request_at.get(domain)
    if last is not None:
        wait = config.MIN_DELAY_PER_DOMAIN_SECONDS - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[domain] = time.monotonic()


class _TextExtractor(html.parser.HTMLParser):
    """Kogub nähtava teksti, jättes vahele skriptid, stiilid ja navigatsiooni.

    Plokitasemel siltide juures lisab reavahetuse, et dokumendi struktuur
    (õigusakti §-d, loetelud) jääks tekstis loetavaks.
    """

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form", "iframe"}
    BLOCK = {
        "p", "div", "br", "tr", "li", "section", "article", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dt", "dd",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = ["".join(self._parts).replace("\xa0", " ")]
        out = []
        for line in "".join(lines).splitlines():
            stripped = " ".join(line.split())
            if stripped:
                out.append(stripped)
        return "\n".join(out)


def clean_html(raw_html: str) -> str:
    """HTML -> loetav lihttekst. Vigase märgistuse puhul ei viska erindit."""
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception as exc:  # katkine HTML ei tohi tsüklit maha võtta
        print(f"[fetch] HTML-i parsimise hoiatus: {exc}")
    return parser.text()


# Tagurpidiühilduv alias — vanad testid ja kutsujad kasutavad seda nime.
_clean_html = clean_html


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_if_changed(url: str, prev_state: dict | None) -> FetchResult:
    """Teeb tingimusliku GET-i ja tagastab, kas sisu muutus.

    prev_state tuleb db.get_page_state() väljundist (None, kui URL-i pole
    varem nähtud).
    """
    headers: dict[str, str] = {}
    if prev_state:
        if prev_state.get("etag"):
            headers["If-None-Match"] = prev_state["etag"]
        if prev_state.get("last_modified"):
            headers["If-Modified-Since"] = prev_state["last_modified"]

    try:
        with http_get(url, headers) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        # urllib VISKAB 304 erindina, kui saatsime tingimusliku päise.
        # See ei ole viga — see tähendab "ei muutunud".
        if exc.code == 304:
            return FetchResult(
                url=url, changed=False, status_code=304,
                etag=(prev_state or {}).get("etag"),
                last_modified=(prev_state or {}).get("last_modified"),
                cleaned_text=None, content_hash=None,
            )
        raise

    cleaned = clean_html(raw)
    digest = content_hash(cleaned)
    changed = digest != (prev_state or {}).get("content_hash")

    return FetchResult(
        url=url,
        changed=changed,
        status_code=status,
        etag=etag,
        last_modified=last_modified,
        cleaned_text=cleaned,
        content_hash=digest,
    )
