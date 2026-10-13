"""
جستجوی ادمین (کادر جستجوی لیست‌ها و کمبوهای autocomplete) که به ی/ي و ک/ك حساس نباشد.

مشکل: هلو متن را با «ي» و «ك» عربی ذخیره می‌کند ولی مدیر با کیبورد فارسی «ی» و «ک» می‌نویسد؛ جستجوی پیش‌فرض جنگو
(icontains) این دو را برابر نمی‌گیرد و کالا/دسته/برند پیدا نمی‌شد. راه‌حل: برای هر فیلدِ متنیِ search_fields، سمت دیتابیس
هر دو حرف عربی به فارسی تبدیل می‌شود (Replace) و عبارت جستجو هم همین‌طور؛ پس هر ترکیبی از املا پیدا می‌شود.

روش نصب: جایگزینی `ModelAdmin.get_search_results` (یک‌بار، از products/apps.py::ready)، تا همه‌ی ادمین‌ها (لیست‌ها و
autocomplete که از همین متد استفاده می‌کند) خودکار پوشش داده شوند. فیلدهای غیرمتنی و پیشوند «@» (جستجوی تمام‌متن) رفتار
قبلی را دارند.
"""

from django.contrib import admin
from django.contrib.admin.utils import lookup_spawns_duplicates
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import F, Value
from django.db.models.constants import LOOKUP_SEP
from django.db.models.functions import Replace
from django.utils.text import smart_split, unescape_string_literal

_ARABIC_TO_PERSIAN = (('ي', 'ی'), ('ك', 'ک'))
_PREFIX_LOOKUPS = {'^': 'istartswith', '=': 'iexact'}


def normalize_term(text):
    """ ي ← ی و ك ← ک (برای مقایسه‌ی املای فارسی/عربی) """
    for arabic, persian in _ARABIC_TO_PERSIAN:
        text = text.replace(arabic, persian)
    return text


def _terminal_field(model, path):
    """ فیلد پایانیِ مسیر «a__b__c» (رابطه‌ها دنبال می‌شوند) یا None اگر مسیر شامل lookup/ناشناخته باشد """
    opts = model._meta
    field = None
    for part in path.split(LOOKUP_SEP):
        if part == 'pk':
            part = opts.pk.name
        try:
            field = opts.get_field(part)
        except FieldDoesNotExist:
            return None
        if hasattr(field, 'path_infos'):
            opts = field.path_infos[-1].to_opts
    return field


def _normalized_expression(path):
    expression = F(path)
    for arabic, persian in _ARABIC_TO_PERSIAN:
        expression = Replace(expression, Value(arabic), Value(persian))
    return expression


def persian_search_results(self, request, queryset, search_term):
    original = _ORIGINAL_GET_SEARCH_RESULTS
    search_fields = self.get_search_fields(request)
    if not search_fields or not search_term:
        return original(self, request, queryset, search_term)

    model = queryset.model
    aliases, text_conditions, other_conditions, specs = {}, [], [], []
    for spec in (str(field) for field in search_fields):
        prefix = spec[0] if spec[:1] in ('^', '=', '@') else ''
        path = spec[1:] if prefix else spec
        specs.append(spec)
        field = None if prefix == '@' else _terminal_field(model, path)
        if isinstance(field, (models.CharField, models.TextField)):
            alias = f'_fa_search_{len(aliases)}'
            aliases[alias] = _normalized_expression(path)
            text_conditions.append(f'{alias}__{_PREFIX_LOOKUPS.get(prefix, "icontains")}')
        elif prefix == '@':
            other_conditions.append(f'{path}__search')
        elif prefix in _PREFIX_LOOKUPS:
            other_conditions.append(f'{path}__{_PREFIX_LOOKUPS[prefix]}')
        else:
            # فیلد غیرمتنی (شناسه/شماره) یا مسیر دارای lookup صریح: همان رفتار جنگو
            other_conditions.append(path if field is None else f'{path}__icontains')
    if not aliases:
        return original(self, request, queryset, search_term)

    queryset = queryset.alias(**aliases)
    term_queries = []
    for bit in smart_split(search_term):
        if bit.startswith(('"', "'")) and bit[0] == bit[-1]:
            bit = unescape_string_literal(bit)
        normalized = normalize_term(bit)
        pairs = [(condition, normalized) for condition in text_conditions]
        pairs += [(condition, bit) for condition in other_conditions]
        term_queries.append(models.Q.create(pairs, connector=models.Q.OR))
    queryset = queryset.filter(models.Q.create(term_queries))
    may_have_duplicates = any(
        lookup_spawns_duplicates(self.opts, spec.lstrip('^=@')) for spec in specs
    )
    return queryset, may_have_duplicates


_current = admin.ModelAdmin.get_search_results
_ORIGINAL_GET_SEARCH_RESULTS = getattr(_current, '__wrapped__', _current)     # reload امن: هرگز خودش را صدا نمی‌زند
persian_search_results.__wrapped__ = _ORIGINAL_GET_SEARCH_RESULTS


def install():
    """ idempotent: اگر قبلاً نصب شده باشد دوباره نمی‌پوشاند """
    if admin.ModelAdmin.get_search_results is not persian_search_results:
        admin.ModelAdmin.get_search_results = persian_search_results
