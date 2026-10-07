import re


def safe_inline(text, limit):
    """ متن کاربر برای پیش‌نمایش و پیامک: بدون خط جدید و لینک (لینک ← «[لینک]»)، با طول محدود """
    value = re.sub(r'(?:https?://|www\.)\S+', '[لینک]', str(text or ''))
    value = re.sub(r'\s+', ' ', value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'
