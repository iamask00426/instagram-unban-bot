import os
import io
import ipaddress
import logging
from io import BytesIO
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BANNED_DP_PATH = os.path.join(BASE_DIR, "default_banned_dp.jpg")
FONT_PATH = os.path.join(BASE_DIR, "font.ttf")

def safe_avatar_url(value):
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.username is None and url.password is None
                and url.port in {None, 443} and not url.fragment
                and any((url.hostname or "").endswith("." + domain)
                        for domain in ("cdninstagram.com", "fbcdn.net")))
    except (ValueError, TypeError):
        return False

class PublicAvatarResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(a["host"]).is_global for a in addresses):
            raise OSError("Non-public avatar address rejected")
        return addresses

    async def close(self):
        await self.resolver.close()

async def download_avatar(url):
    if not safe_avatar_url(url):
        return None
    limit = 2 * 1024 * 1024
    resolver = PublicAvatarResolver()
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, resolver=resolver),
            timeout=aiohttp.ClientTimeout(total=5), cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False, auto_decompress=False,
        ) as session:
            headers = {
                "Accept-Encoding": "identity",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with session.get(url, allow_redirects=True, headers=headers) as response:
                try:
                    if (response.status != 200 or response.headers.get("Content-Encoding", "identity") not in {"", "identity"}
                            or (response.content_length is not None and response.content_length > limit)):
                        return None
                    data = bytearray()
                    while len(data) < limit:
                        chunk = await response.content.read(min(16384, limit - len(data)))
                        if not chunk:
                            return bytes(data)
                        data.extend(chunk)
                    return bytes(data) if response.content.at_eof() else None
                finally:
                    response.close()
    except Exception:
        return None
    finally:
        await resolver.close()

def get_card_fonts():
    """Load bundled TrueType font for cross-platform Linux/Mac rendering"""
    if os.path.exists(FONT_PATH):
        try:
            return {
                "username": ImageFont.truetype(FONT_PATH, 38),
                "btn": ImageFont.truetype(FONT_PATH, 22),
                "stats_num": ImageFont.truetype(FONT_PATH, 28),
                "stats_lbl": ImageFont.truetype(FONT_PATH, 24),
                "name": ImageFont.truetype(FONT_PATH, 24),
            }
        except Exception:
            logging.warning("Unable to load bundled font")

    d = ImageFont.load_default()
    return {"username": d, "btn": d, "stats_num": d, "stats_lbl": d, "name": d}

def format_count(val) -> str:
    """Format numeric counts into Instagram-style k/m/b strings (e.g. 474.4k, 38.4m, 2,681)."""
    if val is None:
        return "0"
    s = str(val).strip().replace(",", "")
    if not s:
        return "0"

    if s[-1].lower() in ("k", "m", "b"):
        num_part = s[:-1].strip()
        suffix = s[-1].lower()
        try:
            f = float(num_part)
            if f.is_integer():
                return f"{int(f)}{suffix}"
            return f"{f:.1f}{suffix}"
        except ValueError:
            return s.lower()

    try:
        num = float(s)
    except ValueError:
        return s

    if num >= 1_000_000_000:
        val_scaled = num / 1_000_000_000
        return f"{val_scaled:.1f}b" if val_scaled % 1 != 0 else f"{int(val_scaled)}b"
    elif num >= 1_000_000:
        val_scaled = num / 1_000_000
        return f"{val_scaled:.1f}m" if val_scaled % 1 != 0 else f"{int(val_scaled)}m"
    elif num >= 10_000:
        val_scaled = num / 1_000
        return f"{val_scaled:.1f}k" if val_scaled % 1 != 0 else f"{int(val_scaled)}k"
    elif num >= 1_000:
        return f"{int(num):,}"
    else:
        return str(int(num))

async def create_profile_card(username, followers, posts, following, pic_url=None, is_unavailable=False, full_name=None, is_verified=False, style=1):
    """Creates a 1000x420 Pure Black High-Res Profile Card with 1:1 parity with Reps bot"""
    width, height = 1000, 420
    img = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(img)

    fonts = get_card_fonts()
    font_btn = fonts["btn"]
    font_stats_num = fonts["stats_num"]
    font_stats_lbl = fonts["stats_lbl"]
    font_name = fonts["name"]

    # Optional banner for Style 4 ("Back from the grave")
    if style == 4 and not is_unavailable:
        draw.rounded_rectangle([280, 80, 670, 125], radius=8, fill='#2a0845')
        draw.text((295, 87), "⚰️ BACK FROM THE GRAVE 🧟‍♂️", font=font_btn, fill='#e084f7')

    av_center = (160, 210)
    av_r = 85
    av_size = (av_r * 2, av_r * 2)

    avatar_drawn = False

    # For an unavailable account, use default_banned_dp.jpg
    if is_unavailable and os.path.exists(DEFAULT_BANNED_DP_PATH):
        try:
            dp_img = Image.open(DEFAULT_BANNED_DP_PATH).convert("RGB").resize(av_size, Image.Resampling.LANCZOS)
            mask = Image.new('L', av_size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
            img.paste(dp_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
            avatar_drawn = True
        except Exception:
            logging.warning("Unable to load default avatar")

    if not avatar_drawn and pic_url:
        try:
            avatar_bytes = await download_avatar(pic_url)
            if avatar_bytes:
                with Image.open(BytesIO(avatar_bytes)) as source:
                    if source.width * source.height <= 16_000_000:
                        av_img = source.convert("RGB").resize(av_size, Image.Resampling.LANCZOS)
                        mask = Image.new('L', av_size, 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
                        img.paste(av_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
                        avatar_drawn = True
        except Exception:
            logging.warning("Avatar unavailable; using local placeholder")

    if not avatar_drawn:
        draw.ellipse([av_center[0]-av_r, av_center[1]-av_r, av_center[0]+av_r, av_center[1]+av_r], fill='#262626')
        first_char = username[0].upper() if username else 'U'
        draw.text((av_center[0]-12, av_center[1]-20), first_char, font=fonts["username"], fill='#ffffff')

    display_username = username if not is_unavailable else "UserNotFound"
    uname_x = 280
    uname_y = 135

    # Dynamically scale username font so [Follow] button and dots never overflow canvas
    max_uname_w = width - uname_x - 170
    cur_uname_size = 38
    font_username = fonts["username"]
    if os.path.exists(FONT_PATH):
        while cur_uname_size > 20 and draw.textlength(display_username, font=font_username) > max_uname_w:
            cur_uname_size -= 2
            font_username = ImageFont.truetype(FONT_PATH, cur_uname_size)

    draw.text((uname_x, uname_y), display_username, font=font_username, fill='#ffffff')

    try:
        uname_w = draw.textlength(display_username, font=font_username)
    except Exception:
        uname_w = len(display_username) * 20

    badge_offset = 0
    if is_verified:
        badge_cx = int(uname_x + uname_w + 18)
        badge_cy = uname_y + 20
        draw.ellipse([badge_cx-11, badge_cy-11, badge_cx+11, badge_cy+11], fill='#0095f6')
        points = [(badge_cx - 5, badge_cy), (badge_cx - 1, badge_cy + 4), (badge_cx + 5, badge_cy - 4)]
        draw.line(points, fill='#ffffff', width=2, joint='curve')
        badge_offset = 30

    btn_left = int(uname_x + uname_w + 22 + badge_offset)
    btn_top = uname_y + 4
    btn_w = 98
    btn_h = 38
    draw.rounded_rectangle([btn_left, btn_top, btn_left+btn_w, btn_top+btn_h], radius=8, fill='#0095f6')
    try:
        btn_txt_w = draw.textlength('Follow', font=font_btn)
    except Exception:
        btn_txt_w = 55
    draw.text((btn_left + (btn_w - btn_txt_w)/2, btn_top + 7), 'Follow', font=font_btn, fill='#ffffff')

    dot_x = btn_left + btn_w + 18
    dot_y = btn_top + 16
    for offset in [0, 11, 22]:
        draw.ellipse([dot_x+offset, dot_y, dot_x+offset+5, dot_y+5], fill='#ffffff')

    posts_str = format_count(posts)
    folls_str = format_count(followers)
    follg_str = format_count(following)
    st_y = 205
    st_lbl_y = 209

    draw.text((uname_x, st_y), posts_str, font=font_stats_num, fill='#ffffff')
    pw = draw.textlength(posts_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(posts_str)*16
    draw.text((uname_x + pw + 8, st_lbl_y), 'posts', font=font_stats_lbl, fill='#a8a8a8')
    pw_lbl = draw.textlength('posts', font=font_stats_lbl) if hasattr(draw, 'textlength') else 60

    foll_x = int(uname_x + pw + 8 + pw_lbl + 28)
    draw.text((foll_x, st_y), folls_str, font=font_stats_num, fill='#ffffff')
    fw = draw.textlength(folls_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(folls_str)*16
    draw.text((foll_x + fw + 8, st_lbl_y), 'followers', font=font_stats_lbl, fill='#a8a8a8')
    fw_lbl = draw.textlength('followers', font=font_stats_lbl) if hasattr(draw, 'textlength') else 95

    follg_x = int(foll_x + fw + 8 + fw_lbl + 28)
    draw.text((follg_x, st_y), follg_str, font=font_stats_num, fill='#ffffff')
    fgw = draw.textlength(follg_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(follg_str)*16
    draw.text((follg_x + fgw + 8, st_lbl_y), 'following', font=font_stats_lbl, fill='#a8a8a8')

    sub_title = (full_name if full_name else username) if not is_unavailable else "UserNotFound"
    name_y = 265
    max_name_w = width - uname_x - 30
    cur_name_size = 24
    if os.path.exists(FONT_PATH):
        while cur_name_size > 16 and draw.textlength(sub_title, font=font_name) > max_name_w:
            cur_name_size -= 2
            font_name = ImageFont.truetype(FONT_PATH, cur_name_size)
    while hasattr(draw, 'textlength') and draw.textlength(sub_title, font=font_name) > max_name_w and len(sub_title) > 3:
        sub_title = sub_title[:-4] + "…"

    draw.text((uname_x, name_y), sub_title, font=font_name, fill='#ffffff')

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio
