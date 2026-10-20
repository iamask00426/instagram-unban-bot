from __future__ import annotations

"""Import-safe configuration; environment and .env are read only at startup."""

from dataclasses import dataclass
import math
import re
from typing import Optional
from urllib.parse import quote, unquote, urlsplit


USERNAME = re.compile(r"[a-zA-Z0-9_](?:[a-zA-Z0-9_.]{0,28}[a-zA-Z0-9_])?\Z")


def extract_username(token: str) -> str | None:
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None

    # Strip surrounding markdown, brackets, quotes, angle brackets, punctuation
    token = token.strip("<>\"'()[]{}.,;`")

    lower_token = token.lower()
    if "instagram.com/" in lower_token or "instagr.am/" in lower_token:
        if not (lower_token.startswith("http://") or lower_token.startswith("https://")):
            token = "https://" + token
        try:
            parsed = urlsplit(token)
            path = parsed.path.strip("/")
            parts = [p for p in path.split("/") if p]
            if not parts:
                return None
            candidate = parts[0]
            if candidate.lower() == "stories" and len(parts) > 1 and parts[1].lower() != "highlights":
                candidate = parts[1]
            elif candidate.lower() in {"p", "reel", "reels", "stories", "tv", "explore", "direct", "accounts", "about", "developer", "legal"}:
                return None
            token = candidate
        except Exception:
            return None

    # Strip query/fragments and trailing/leading slashes
    token = token.split("?")[0].split("&")[0].split("#")[0].strip("/")

    # Strip leading @ or /
    token = token.lstrip("@/").strip()
    token = token.lower()

    if re.fullmatch(r"[a-z0-9_.]{1,30}", token) and ".." not in token:
        token = token.strip(".")
        if token and re.fullmatch(r"[a-z0-9_.]{1,30}", token):
            return token
    return None


def normalize_username(value: str) -> str:
    cleaned = extract_username(value)
    if not cleaned:
        raise ValueError("Use a valid Instagram username or profile link.")
    return cleaned


def is_example(value: str) -> bool:
    value = unquote(value).strip().lower()
    return (not value or value == "optional_instagram_session_cookie_here" or any(word in value for word in
            ("your_", "your-", "example", "placeholder", "changeme", "replace_me"))
            or value.startswith("123456789:"))


def parse_proxy_entry(entry: str) -> str:
    entry = entry.strip()
    if not entry:
        raise ValueError("PROXIES contains an empty entry.")
    if "://" not in entry:
        parts = entry.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            entry = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        elif len(parts) == 2:
            entry = "http://" + entry
        else:
            raise ValueError("Invalid PROXIES entry; use an HTTP(S) proxy URL.")
    try:
        url = urlsplit(entry)
        valid = (url.scheme in {"http", "https"} and url.hostname and url.port
                 and url.path in {"", "/"} and not url.query and not url.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in entry))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid PROXIES entry; only HTTP(S) proxies are supported, not SOCKS.")
    return entry


def _number(env, key, default, low, high, integer=False):
    try:
        value = int(env.get(key, default)) if integer else float(env.get(key, default))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError(f"{key} must be between {low} and {high}.") from None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    proxies: tuple[str, ...]
    mode: str = "html"
    session_id: str = ""
    check_interval: float = 2.0
    concurrency: int = 10
    body_limit: int = 128 * 1024
    request_timeout: float = 10.0
    backoff_base: float = 10.0
    backoff_max: float = 300.0
    unavailable_confirmations: int = 2
    confirmation_interval: float = 5.0
    alert_backoff_base: float = 5.0
    alert_backoff_max: float = 300.0

    @classmethod
    def from_env(cls, env):
        token = env.get("BOT_TOKEN", "").strip()
        if is_example(token) or not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Set BOT_TOKEN to an explicit valid Telegram bot token.")
        raw_proxies = env.get("PROXIES", "").strip()
        if raw_proxies:
            proxies = tuple(parse_proxy_entry(p) for p in re.split(r"[,;\r\n]+", raw_proxies) if p.strip())
        else:
            proxies = ()
        mode = env.get("IG_CHECK_MODE", "html").strip().lower()
        if mode not in {"html", "json"}:
            raise ValueError("IG_CHECK_MODE must be html or json.")
        session_id = env.get("IG_SESSIONID", "").strip()
        if is_example(session_id):
            session_id = ""
        if any(ord(c) < 33 or c in ";," for c in session_id):
            raise ValueError("IG_SESSIONID must be a single cookie value.")
        if mode == "json" and not session_id:
            raise ValueError("JSON mode requires a real IG_SESSIONID, not an example placeholder.")
        base = _number(env, "BACKOFF_BASE_SECONDS", 10, 1, 3600)
        maximum = _number(env, "BACKOFF_MAX_SECONDS", 300, base, 86400)
        return cls(
            bot_token=token, proxies=proxies, mode=mode,
            session_id=session_id if mode == "json" else "",
            check_interval=_number(env, "CHECK_INTERVAL_SECONDS", 2, 0.1, 3600),
            concurrency=_number(env, "CHECK_CONCURRENCY", 10, 1, 100, True),
            body_limit=_number(env, "PROFILE_BODY_LIMIT_BYTES", 131072, 1024, 1048576, True),
            request_timeout=_number(env, "REQUEST_TIMEOUT_SECONDS", 10, 1, 120),
            backoff_base=base, backoff_max=maximum,
            unavailable_confirmations=_number(env, "UNAVAILABLE_CONFIRMATIONS", 2, 2, 10, True),
            confirmation_interval=_number(env, "CONFIRMATION_INTERVAL_SECONDS", 5, 1, 3600),
        )

def parse_usernames(text: str) -> list[str]:
    """
    Extract ONLY valid Instagram usernames from multiline text, URLs, or handles.
    """
    if not text:
        return []
    raw_tokens = re.split(r'[\r\n,\s]+', str(text))
    usernames = []
    seen = set()

    for token in raw_tokens:
        clean = extract_username(token)
        if clean and clean not in seen:
            seen.add(clean)
            usernames.append(clean)

    return usernames

