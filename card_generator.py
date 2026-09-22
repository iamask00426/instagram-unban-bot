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
                "username": ImageFont.truetype(FONT_PATH, 48),
                "btn": ImageFont.truetype(FONT_PATH, 26),
                "stats_num": ImageFont.truetype(FONT_PATH, 34),
                "stats_lbl": ImageFont.truetype(FONT_PATH, 30),
                "name": ImageFont.truetype(FONT_PATH, 32),
            }
        except Exception:
            logging.warning("Unable to load bundled font")

    d = ImageFont.load_default()
    return {"username": d, "btn": d, "stats_num": d, "stats_lbl": d, "name": d}

async def create_profile_card(username, followers, posts, following, pic_url=None, is_unavailable=False, full_name=None):
    """Creates a 1000x550 Pure Black High-Res Profile Card with zero-overlap dynamic layout"""
    width, height = 1000, 550
    img = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(img)

    fonts = get_card_fonts()
    font_username = fonts["username"]
    font_btn = fonts["btn"]
    font_stats_num = fonts["stats_num"]
    font_stats_lbl = fonts["stats_lbl"]
    font_name = fonts["name"]

    av_center = (180, 275)
    av_r = 100
    av_size = (av_r * 2, av_r * 2)

    avatar_drawn = False

    # For an unavailable account, use default_banned_dp.jpg
    if is_unavailable and os.path.exists(DEFAULT_BANNED_DP_PATH):
        try:
            dp_img = Image.open(DEFAULT_BANNED_DP_PATH).convert("RGB").resize(av_size)
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
                        av_img = source.convert("RGB").resize(av_size)
                        mask = Image.new('L', av_size, 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
                        img.paste(av_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
                        avatar_drawn = True
        except Exception:
            logging.warning("Avatar unavailable; using local placeholder")

    if not avatar_drawn:
        draw.ellipse([av_center[0]-av_r, av_center[1]-av_r, av_center[0]+av_r, av_center[1]+av_r], fill='#262626')
        first_char = username[0].upper() if username else 'U'
        draw.text((av_center[0]-15, av_center[1]-25), first_char, font=font_username, fill='#ffffff')

    display_username = username if not is_unavailable else "UserNotFound"
    uname_x = 320
    uname_y = 200
    draw.text((uname_x, uname_y), display_username, font=font_username, fill='#ffffff')

    try:
        uname_w = draw.textlength(display_username, font=font_username)
    except Exception:
        uname_w = len(display_username) * 28

    btn_left = int(uname_x + uname_w + 25)
    btn_top = 205
    btn_w = 130
    btn_h = 50
    draw.rounded_rectangle([btn_left, btn_top, btn_left+btn_w, btn_top+btn_h], radius=10, fill='#0095f6')
    draw.text((btn_left+26, btn_top+10), 'Follow', font=font_btn, fill='#ffffff')

    dot_x = btn_left + btn_w + 25
    dot_y = btn_top + 22
    for offset in [0, 14, 28]:
        draw.ellipse([dot_x+offset, dot_y, dot_x+offset+7, dot_y+7], fill='#ffffff')

    posts_str = str(posts)
    folls_str = str(followers)
    follg_str = str(following)
    st_y = 275
    st_lbl_y = 278

    draw.text((320, st_y), posts_str, font=font_stats_num, fill='#ffffff')
    pw = draw.textlength(posts_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(posts_str)*20
    draw.text((320 + pw + 10, st_lbl_y), 'posts', font=font_stats_lbl, fill='#a8a8a8')
    pw_lbl = draw.textlength('posts', font=font_stats_lbl) if hasattr(draw, 'textlength') else 75

    foll_x = int(320 + pw + 10 + pw_lbl + 35)
    draw.text((foll_x, st_y), folls_str, font=font_stats_num, fill='#ffffff')
    fw = draw.textlength(folls_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(folls_str)*20
    draw.text((foll_x + fw + 10, st_lbl_y), 'followers', font=font_stats_lbl, fill='#a8a8a8')
    fw_lbl = draw.textlength('followers', font=font_stats_lbl) if hasattr(draw, 'textlength') else 120

    follg_x = int(foll_x + fw + 10 + fw_lbl + 35)
    draw.text((follg_x, st_y), follg_str, font=font_stats_num, fill='#ffffff')
    fgw = draw.textlength(follg_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(follg_str)*20
    draw.text((follg_x + fgw + 10, st_lbl_y), 'following', font=font_stats_lbl, fill='#a8a8a8')

    sub_title = (full_name if full_name else username) if not is_unavailable else "UserNotFound"
    draw.text((320, 330), sub_title, font=font_name, fill='#ffffff')

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio
