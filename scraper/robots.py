"""
Väike robots.txt hindaja.

MIKS MITTE urllib.robotparser: stdlib parser ei oska kahte asja, mida meie
allikad päriselt kasutavad.

1. Metamärgid tee KESKEL. `RuleLine.applies_to` on lihtne `startswith`, seega
   riigiteataja.ee reegel `Disallow: */kohtulahendid/*` ei kattu MITTE KUNAGI
   ühegi URL-iga — parser lubaks kõik.
2. Tühjad read grupi sees. Parser kohtleb tühja rida kirje lõpuna; eis.ee
   robots.txt-s on 'User-agent: *' grupi sees tühje ridu ja kommentaare, mille
   järel KÕIK reeglid (/wp-admin/, /toetatud-projektid) jäävad orvuks.

See moodul teeb selle, mida robots.txt de facto standard (RFC 9309) ütleb:
grupi valik user-agendi järgi, `*` ja `$` metamärgid, ning pikima kattuva
reegli võit, kus võrdse pikkuse korral Allow trumpab Disallow'd.
"""

import re
from urllib.parse import urlparse


class Rules:
    """Ühe domeeni robots.txt reeglid, juba meie user-agendi jaoks valitud."""

    def __init__(self, allow: list[str], disallow: list[str]):
        self._allow = [(p, _compile(p)) for p in allow]
        self._disallow = [(p, _compile(p)) for p in disallow]

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        best_allow = _longest_match(self._allow, path)
        best_disallow = _longest_match(self._disallow, path)

        if best_disallow is None:
            return True
        if best_allow is None:
            return False
        # Võrdse täpsuse korral võidab Allow — nii ütleb standard ja nii
        # käitub ka eis.ee 'Allow: /wp-admin/admin-ajax.php' erand.
        return best_allow >= best_disallow


def _longest_match(rules, path: str) -> int | None:
    """Pikima kattuva mustri pikkus, või None kui ükski ei kattu."""
    best = None
    for pattern, regex in rules:
        if regex.match(path):
            length = len(pattern)
            if best is None or length > best:
                best = length
    return best


def _compile(pattern: str) -> re.Pattern:
    """robots.txt tee-muster -> regex. `*` = suvaline jupp, `$` lõpus = ankur."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    escaped = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile(escaped + ("$" if anchored else ""))


def parse(body: str, user_agent: str) -> Rules:
    """Parsib robots.txt ja tagastab reeglid, mis kehtivad user_agent'i kohta.

    Grupi valik: täpseim (pikim) user-agent, mille nimi sisaldub meie omas;
    muidu '*' grupp. Tühje ridu EI kasutata grupi piirina — grupp lõpeb siis,
    kui algab uus 'User-agent:' rida pärast reegleid.
    """
    token = user_agent.split("/")[0].strip().lower()

    groups: list[tuple[list[str], list[str], list[str]]] = []  # (agents, allow, disallow)
    agents: list[str] = []
    allow: list[str] = []
    disallow: list[str] = []
    expecting_agents = False

    def flush():
        if agents:
            groups.append((list(agents), list(allow), list(disallow)))

    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if not expecting_agents:  # eelmine grupp sai otsa
                flush()
                agents, allow, disallow = [], [], []
                expecting_agents = True
            agents.append(value.lower())
        elif field in ("allow", "disallow"):
            expecting_agents = False
            if not agents:
                continue  # reegel ilma grupita — jäta vahele
            if field == "disallow":
                if value:  # tühi Disallow tähendab "luba kõik", mitte reeglit
                    disallow.append(value)
            else:
                allow.append(value)
    flush()

    best: tuple[list[str], list[str]] | None = None
    best_len = -1
    fallback: tuple[list[str], list[str]] | None = None
    for group_agents, group_allow, group_disallow in groups:
        for agent in group_agents:
            if agent == "*":
                if fallback is None:
                    fallback = (group_allow, group_disallow)
            elif agent in token and len(agent) > best_len:
                best, best_len = (group_allow, group_disallow), len(agent)

    chosen = best or fallback or ([], [])
    return Rules(*chosen)
