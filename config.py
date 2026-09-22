"""Import-safe configuration; environment and .env are read only at startup."""

from dataclasses import dataclass
import math
import re
from urllib.parse import quote, unquote, urlsplit


USERNAME = re.compile(r"[a-zA-Z0-9_](?:[a-zA-Z0-9_.]{0,28}[a-zA-Z0-9_])?\Z")


def normalize_username(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlsplit(value)
        if (url.hostname not in {"instagram.com", "www.instagram.com"}
                or url.netloc != url.hostname or url.query or url.fragment):
            raise ValueError("Use a username or a plain Instagram profile URL.")
        value = url.path.strip("/")
    elif value.startswith(("instagram.com/", "www.instagram.com/")):
        return normalize_username("https://" + value)
    elif value.startswith("@"):
        value = value[1:]
    if not USERNAME.fullmatch(value) or ".." in value:
        raise ValueError("Username must be 1–30 letters, digits, underscores or internal dots.")
    return value.lower()


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
        if not raw_proxies:
            raise ValueError("Set PROXIES explicitly; direct Instagram fallback is disabled.")
        proxies = tuple(parse_proxy_entry(p) for p in re.split(r"[,;\r\n]+", raw_proxies) if p.strip())
        if not proxies:
            raise ValueError("Set at least one HTTP(S) proxy in PROXIES.")
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
    raw_tokens = re.split(r'[\r\n,\s]+', text)
    usernames = []
    seen = set()

    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue

        # If it's an explicit URL
        if token.startswith(('http://', 'https://')):
            try:
                parsed = urlsplit(token)
                if parsed.hostname not in {'instagram.com', 'www.instagram.com'}:
                    continue
                path = parsed.path.strip('/')
                parts = [p for p in path.split('/') if p]
                if parts:
                    token = parts[0]
                else:
                    continue
            except Exception:
                continue
        elif 'instagram.com/' in token:
            try:
                parsed = urlsplit('https://' + token)
                path = parsed.path.strip('/')
                parts = [p for p in path.split('/') if p]
                if parts:
                    token = parts[0]
                else:
                    continue
            except Exception:
                continue

        token = token.split('?')[0].split('&')[0].strip('/')
        if token.startswith('@'):
            token = token[1:]

        token = token.lower()
        if re.fullmatch(r'[a-zA-Z0-9_.]{1,30}', token):
            if token not in seen:
                seen.add(token)
                usernames.append(token)

    return usernames
