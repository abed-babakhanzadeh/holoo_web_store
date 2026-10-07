"""
ماشین وضعیت گفتگو (طرح مهندسی، نسخه‌ی ۲): پنج وضعیت و ماتریس انتقال‌های مجاز. «ماتریس» یک ساختار داده است (RULES)؛ هر انتقال فقط
با شناسه‌ی قاعده (T1 ... T20) و عامل (مشتری/کارشناس/سیستم) و از وضعیت مبدأ مجاز انجام می‌شود، وگرنه InvalidTransition.

معنای وضعیت‌ها:
  waiting_operator  در صف: کارشناسی گفتگو را برنداشته
  active            کارشناس مسئول دارد و گفتگو در جریان است
  waiting_customer  کارشناس پاسخ داده و توپ در زمین مشتری است
  offline           حالت ناهمزمان (پیام آفلاین، یا SLA/غیبت مشتری منقضی شده)
  closed            بسته

حضور کارشناس (Presence) هرگز وضعیت را عوض نمی‌کند؛ فقط تایمرها (SLA، بی‌پاسخی مشتری، ...) و اقدام صریح کارشناس/مشتری.
T20 (مشتری در حالت صف پیام می‌دهد) تکمیلِ ماتریس تأییدشده است: در Rev2 فقط از قلم افتاده بود.
"""
from dataclasses import dataclass
from datetime import timedelta

WAITING_OPERATOR = 'waiting_operator'
ACTIVE = 'active'
WAITING_CUSTOMER = 'waiting_customer'
OFFLINE = 'offline'
CLOSED = 'closed'

OPEN_STATES = frozenset({WAITING_OPERATOR, ACTIVE, WAITING_CUSTOMER, OFFLINE})
ALL_STATES = OPEN_STATES | {CLOSED}

CUSTOMER = 'customer'
OPERATOR = 'operator'
SYSTEM = 'system'
ACTORS = (CUSTOMER, OPERATOR, SYSTEM)

TIMER_SLA = 'sla'
TIMER_CUSTOMER_IDLE = 'customer_idle'
TIMER_CUSTOMER_GONE = 'customer_gone'
TIMER_IDLE_CLOSE = 'idle_close'

NEW = None          # «مبدأ» برای ساخت گفتگوی تازه (∅)


class InvalidTransition(Exception):
    """ این انتقال با این عامل از این وضعیت مجاز نیست (ماتریس RULES). """


class TransitionConflict(Exception):
    """ وضعیت گفتگو بین خواندن و نوشتن عوض شد (مسابقه)؛ فراخوان‌کننده باید دوباره بخواند. """


@dataclass(frozen=True)
class Rule:
    id: str
    sources: frozenset
    target: str
    actors: frozenset
    title: str


def _rule(rule_id, sources, target, actors, title):
    return Rule(rule_id, frozenset(sources), target, frozenset(actors), title)


RULES = {r.id: r for r in (
    _rule('T1', {NEW}, WAITING_OPERATOR, {CUSTOMER}, 'ساخت گفتگوی زنده'),
    _rule('T2', {NEW}, OFFLINE, {CUSTOMER}, 'ساخت پیام آفلاین'),
    _rule('T3', {WAITING_OPERATOR}, ACTIVE, {OPERATOR}, 'برداشتن گفتگو / اولین پاسخ'),
    _rule('T4', {ACTIVE}, ACTIVE, {OPERATOR}, 'پاسخ کارشناس'),
    _rule('T5', {ACTIVE}, ACTIVE, {CUSTOMER}, 'پیام مشتری'),
    _rule('T6', {ACTIVE}, WAITING_CUSTOMER, {SYSTEM, OPERATOR}, 'در انتظار مشتری'),
    _rule('T7', {WAITING_CUSTOMER}, ACTIVE, {CUSTOMER}, 'بازگشت مشتری (دارای مسئول)'),
    _rule('T7b', {WAITING_CUSTOMER}, WAITING_OPERATOR, {CUSTOMER}, 'بازگشت مشتری (بدون مسئول)'),
    _rule('T8', {WAITING_CUSTOMER}, OFFLINE, {SYSTEM}, 'رفتن مشتری'),
    _rule('T9', {WAITING_OPERATOR}, OFFLINE, {SYSTEM}, 'انقضای SLA در صف'),
    _rule('T10', {ACTIVE}, OFFLINE, {SYSTEM}, 'انقضای SLA بدون پاسخ'),
    _rule('T11', {OFFLINE}, WAITING_OPERATOR, {CUSTOMER}, 'بازگشت به صف (کارشناس در دسترس)'),
    _rule('T12', {OFFLINE}, OFFLINE, {CUSTOMER}, 'پیام مشتری در حالت ناهمزمان'),
    _rule('T13', {OFFLINE}, WAITING_CUSTOMER, {OPERATOR}, 'پاسخ کارشناس به گفتگوی ناهمزمان'),
    _rule('T14', {WAITING_CUSTOMER}, WAITING_CUSTOMER, {OPERATOR}, 'پیگیری کارشناس'),
    _rule('T15', {ACTIVE, WAITING_CUSTOMER}, WAITING_OPERATOR, {OPERATOR, SYSTEM}, 'بازگرداندن به صف'),
    _rule('T16', {ACTIVE}, ACTIVE, {OPERATOR}, 'ارجاع به کارشناس دیگر'),
    _rule('T17', OPEN_STATES, CLOSED, {CUSTOMER, OPERATOR, SYSTEM}, 'بستن گفتگو'),
    _rule('T18', {CLOSED}, WAITING_OPERATOR, {CUSTOMER, OPERATOR}, 'بازگشایی گفتگو'),
    _rule('T20', {WAITING_OPERATOR}, WAITING_OPERATOR, {CUSTOMER}, 'پیام مشتری در صف'),
)}

# انتقال‌هایی که هیچ قاعده‌ای نباید بسازد (تست: هیچ ترکیب (مبدأ، مقصد) زیر در RULES نیست)
FORBIDDEN_PAIRS = frozenset({
    (NEW, ACTIVE), (NEW, WAITING_CUSTOMER), (NEW, CLOSED),
    (CLOSED, ACTIVE), (CLOSED, WAITING_CUSTOMER), (CLOSED, OFFLINE),
    (WAITING_OPERATOR, WAITING_CUSTOMER),
    (OFFLINE, ACTIVE),
})


def check(rule_id, source, actor):
    """ قاعده را برمی‌گرداند یا InvalidTransition می‌اندازد """
    rule = RULES.get(rule_id)
    if rule is None:
        raise InvalidTransition(f'قاعده‌ی {rule_id} وجود ندارد.')
    if source not in rule.sources:
        raise InvalidTransition(f'{rule_id} از وضعیت «{source}» مجاز نیست.')
    if actor not in rule.actors:
        raise InvalidTransition(f'{rule_id} برای عامل «{actor}» مجاز نیست.')
    return rule


def allowed_rules(source, actor):
    """ قاعده‌های مجاز برای این عامل از این وضعیت (برای نمایش دکمه‌های پیشخوان) """
    return sorted((r.id for r in RULES.values() if source in r.sources and actor in r.actors),
                  key=lambda x: (len(x), x))


# ------------------------------------------------------------------ تایمرها

def _minutes(value):
    return timedelta(minutes=int(value or 0))


def timer_for(rule_id, now, cfg, current_kind='', current_at=None):
    """
    تایمر بعد از این قاعده: (موعد یا None، نوع). مقادیر از SiteSettings (cfg). فاز ۲ فقط موعد را نگه می‌دارد؛ جاروی Beat که آن را
    اجرا کند در فاز ۳ می‌آید.
    """
    sla = _minutes(cfg.chat_operator_response_sla_minutes)
    idle_close = timedelta(hours=int(cfg.chat_idle_close_hours or 0))

    def sla_timer():
        return (now + sla, TIMER_SLA) if sla else (None, '')

    if rule_id in ('T1', 'T7', 'T7b', 'T11', 'T15', 'T18'):
        return sla_timer()
    if rule_id in ('T5', 'T20'):
        # SLA در حال اجرا تمدید نمی‌شود (قدیمی‌ترین پیام بی‌پاسخ مبناست)
        if current_kind == TIMER_SLA and current_at:
            return current_at, TIMER_SLA
        return sla_timer()
    if rule_id in ('T3', 'T4'):
        return now + _minutes(cfg.chat_customer_idle_minutes), TIMER_CUSTOMER_IDLE
    if rule_id in ('T6', 'T14'):
        return now + _minutes(cfg.chat_customer_gone_minutes), TIMER_CUSTOMER_GONE
    if rule_id in ('T2', 'T8', 'T9', 'T10', 'T12', 'T13'):
        return now + idle_close, TIMER_IDLE_CLOSE
    if rule_id == 'T17':
        return None, ''
    return current_at, current_kind                    # T16: ارجاع، تایمر دست‌نخورده
