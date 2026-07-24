import io
import random
import secrets

from PIL import Image, ImageDraw, ImageFont

CAPTCHA_DIGITS = 5
_SESSION_PREFIX = 'captcha_'


def new_captcha(request):
    """ یک کد عددی تصادفی می‌سازد، در سشن ذخیره می‌کند و کلیدش را برمی‌گرداند """
    code = ''.join(str(random.randint(0, 9)) for _ in range(CAPTCHA_DIGITS))
    key = secrets.token_urlsafe(8)
    request.session[_SESSION_PREFIX + key] = code
    return key


def get_captcha_code(request, key):
    return request.session.get(_SESSION_PREFIX + key)


def verify_captcha(request, key, answer):
    """ یک‌بارمصرف: مقدار صحیح را از سشن pop می‌کند (چه پاسخ درست بود چه غلط) تا کد قابل حدس‌زدن مکرر نباشد """
    if not key:
        return False
    expected = request.session.pop(_SESSION_PREFIX + key, None)
    return bool(expected) and bool(answer) and expected == answer.strip()


def _load_font(size):
    try:
        return ImageFont.truetype('arial.ttf', size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def render_captcha_png(code):
    """ کد عددی را به یک تصویر PNG ساده با کمی چرخش/نویز تبدیل می‌کند (بدون وابستگی خارجی) """
    width, height = 150, 50
    image = Image.new('RGB', (width, height), color=(245, 246, 248))
    draw = ImageDraw.Draw(image)

    for _ in range(5):
        start = (random.randint(0, width), random.randint(0, height))
        end = (random.randint(0, width), random.randint(0, height))
        draw.line([start, end], fill=(205, 208, 214), width=1)

    font = _load_font(30)
    char_width = width // len(code)
    for i, ch in enumerate(code):
        char_image = Image.new('RGBA', (char_width, height), (0, 0, 0, 0))
        char_draw = ImageDraw.Draw(char_image)
        char_draw.text((char_width // 4, 6), ch, font=font, fill=(55, 60, 75))
        rotated = char_image.rotate(random.randint(-25, 25), expand=0)
        image.paste(rotated, (i * char_width, 0), rotated)

    for _ in range(60):
        x, y = random.randint(0, width - 1), random.randint(0, height - 1)
        draw.point((x, y), fill=(190, 192, 200))

    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()
