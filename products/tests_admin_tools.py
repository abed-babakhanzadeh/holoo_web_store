"""
ابزارهای ادمین محصولات (۱۳ اکتبر ۲۰۲۶): جستجوی حساس‌نبودن به ی/ي و ک/ك (لیست‌ها و autocomplete)، تخصیص دسته‌جمعی برند،
و «حذف کامل از سایت» برای دسته‌ها (دسته، زیردسته‌ها، کالاها، سینک هلو، نمایش فروشگاه).
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from holoo.tasks import sync_products_from_holoo
from products import home_cache
from products.models import Brand, Category, Product

_n = [0]


def make_product(name, category=None, brand=None, price=100000, **extra):
    _n[0] += 1
    return Product.objects.create(name=name, slug=f'admin-tools-p{_n[0]}', erp_code=f'ERP-AT-{_n[0]}', category=category,
                                  brand=brand, price=price, stock=5, **extra)


class AdminBase(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09120000901', password='x')
        self.client.force_login(self.admin_user)


class PersianInsensitiveSearchTests(AdminBase):
    def setUp(self):
        super().setUp()
        # هلو حروف عربی ذخیره می‌کند: «ي» و «ك»
        self.arabic = make_product('رژ لب كرمي مات')
        self.persian = make_product('رژ لب کرمی براق')
        self.other = make_product('شامپو کودک')

    def search(self, term):
        response = self.client.get(reverse('admin:products_product_changelist'), {'q': term})
        self.assertEqual(response.status_code, 200)
        return {obj.pk for obj in response.context['cl'].result_list}

    def test_persian_keyboard_finds_arabic_stored_names_and_vice_versa(self):
        found = self.search('کرمی')                 # ک و ی فارسی
        self.assertEqual(found, {self.arabic.pk, self.persian.pk})
        found = self.search('كرمي')                 # ك و ي عربی
        self.assertEqual(found, {self.arabic.pk, self.persian.pk})

    def test_multi_word_search_requires_every_word(self):
        self.assertEqual(self.search('رژ كرمی'), {self.arabic.pk, self.persian.pk})
        self.assertEqual(self.search('رژ کودک'), set())

    def test_search_on_other_fields_still_works(self):
        self.assertEqual(self.search(self.other.erp_code), {self.other.pk})

    def test_related_field_search_is_normalised_too(self):
        from products.admin import ProductAdmin
        self.assertIn('name', ProductAdmin.search_fields)         # فیلد متنی مستقیم
        brand = Brand.objects.create(name='ليدي ک', slug='b-lady')
        product = make_product('کالای برند', brand=brand)
        response = self.client.get(reverse('admin:products_product_changelist'), {'brand__id__exact': brand.pk})
        self.assertEqual({o.pk for o in response.context['cl'].result_list}, {product.pk})

    def test_autocomplete_combo_finds_arabic_brand_names(self):
        brand = Brand.objects.create(name='كيان', slug='b-kian')
        response = self.client.get(reverse('admin:autocomplete'), {
            'term': 'کیان', 'app_label': 'products', 'model_name': 'product', 'field_name': 'brand'})
        self.assertEqual(response.status_code, 200)
        names = [item['text'] for item in response.json()['results']]
        self.assertIn(brand.name, names)

    def test_category_admin_search_is_normalised(self):
        category = Category.objects.create(name='مراقبت پوستي', slug='cat-skin')
        response = self.client.get(reverse('admin:products_category_changelist'), {'q': 'پوستی'})
        self.assertIn(category.pk, {o.pk for o in response.context['cl'].result_list})


class BulkBrandAssignmentTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.brand = Brand.objects.create(name='برند هدف', slug='target-brand')
        self.old_brand = Brand.objects.create(name='برند قدیمی', slug='old-brand')
        self.p1 = make_product('رژ لب الف')
        self.p2 = make_product('رژ لب ب', brand=self.old_brand)
        self.p3 = make_product('کرم دست')
        self.url = reverse('admin:products_product_changelist')

    def post(self, ids, follow=False, **extra):
        data = {'action': 'assign_brand_action', '_selected_action': ids, **extra}
        return self.client.post(self.url, data, follow=follow)

    def test_action_shows_the_confirmation_page_with_count_and_sample(self):
        response = self.post([self.p1.pk, self.p2.pk])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="assign-brand-form"')
        self.assertContains(response, 'رژ لب الف')
        self.assertContains(response, '<strong>2</strong>', html=False)
        Product.objects.get(pk=self.p1.pk)
        self.assertIsNone(Product.objects.get(pk=self.p1.pk).brand)             # هنوز اعمال نشده

    def test_apply_sets_the_brand_only_on_selected_products_and_clears_the_home_cache(self):
        cache.set(home_cache.POPULAR_BRAND_IDS, [999], 600)
        response = self.post([self.p1.pk, self.p2.pk], apply='1', brand=self.brand.pk, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.get(pk=self.p1.pk).brand_id, self.brand.pk)
        self.assertEqual(Product.objects.get(pk=self.p2.pk).brand_id, self.brand.pk)
        self.assertIsNone(Product.objects.get(pk=self.p3.pk).brand)
        self.assertContains(response, '2 محصول')
        self.assertIsNone(cache.get(home_cache.POPULAR_BRAND_IDS))

    def test_invalid_brand_keeps_the_page_and_changes_nothing(self):
        response = self.post([self.p1.pk], apply='1', brand='')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="assign-brand-form"')
        self.assertIsNone(Product.objects.get(pk=self.p1.pk).brand)

    def test_select_across_applies_to_the_whole_search_result(self):
        response = self.client.post(self.url + '?q=' + 'رژ', {
            'action': 'assign_brand_action', 'select_across': '1', 'index': '0', '_selected_action': [self.p1.pk],
            'apply': '1', 'brand': self.brand.pk,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.get(pk=self.p1.pk).brand_id, self.brand.pk)
        self.assertEqual(Product.objects.get(pk=self.p2.pk).brand_id, self.brand.pk)      # در نتیجه‌ی جستجو، تیک نخورده
        self.assertIsNone(Product.objects.get(pk=self.p3.pk).brand)                      # خارج از نتیجه‌ی جستجو

    def test_clear_brand_action(self):
        self.client.post(self.url, {'action': 'clear_brand_action', '_selected_action': [self.p2.pk]}, follow=True)
        self.assertIsNone(Product.objects.get(pk=self.p2.pk).brand)

    def test_without_brand_filter(self):
        response = self.client.get(self.url, {'brand_state': 'none'})
        ids = {o.pk for o in response.context['cl'].result_list}
        self.assertEqual(ids, {self.p1.pk, self.p3.pk})


class ExcludedCategoryTests(AdminBase):
    def setUp(self):
        super().setUp()
        cache.delete('lock:holoo:product_sync')
        self.root = Category.objects.create(name='خدمات', slug='svc-root', erp_code='GRP-SVC')
        self.child = Category.objects.create(name='نصب', slug='svc-child', erp_code='GRP-SVC-1', parent=self.root)
        self.shop = Category.objects.create(name='آرایشی', slug='shop-cat', erp_code='GRP-SHOP')

    def test_flag_applies_to_subtree_and_deactivates(self):
        self.root.exclude_from_site = True
        self.root.save()
        self.child.refresh_from_db()
        self.root.refresh_from_db()
        self.assertTrue(self.child.exclude_from_site)
        self.assertFalse(self.child.is_active)
        self.assertFalse(self.root.is_active)

    def test_an_excluded_category_cannot_be_reactivated_by_saving(self):
        self.root.exclude_from_site = True
        self.root.save()
        self.root.is_active = True
        self.root.save()
        self.root.refresh_from_db()
        self.assertFalse(self.root.is_active)

    def test_unflagging_restores_the_subtree(self):
        self.root.exclude_from_site = True
        self.root.save()
        self.root.exclude_from_site = False
        self.root.is_active = True
        self.root.save()
        self.child.refresh_from_db()
        self.assertFalse(self.child.exclude_from_site)
        self.assertTrue(self.child.is_active)

    def test_new_child_under_an_excluded_parent_inherits_the_flag(self):
        self.root.exclude_from_site = True
        self.root.save()
        grandchild = Category.objects.create(name='تعمیر', slug='svc-grand', parent=self.child)
        self.assertTrue(grandchild.exclude_from_site)
        self.assertFalse(grandchild.is_active)

    def test_products_of_an_excluded_category_are_invisible_everywhere(self):
        product = make_product('خدمت نصب', category=self.child)
        keep = make_product('رژ لب', category=self.shop)
        self.assertEqual(set(Product.visible.values_list('pk', flat=True)), {product.pk, keep.pk})
        self.root.exclude_from_site = True
        self.root.save()
        self.assertEqual(set(Product.visible.values_list('pk', flat=True)), {keep.pk})
        self.assertEqual(self.client.get(reverse('products:product_detail', args=[product.slug])).status_code, 404)
        response = self.client.get(reverse('products:product_list'), {'category': self.child.slug})
        self.assertNotContains(response, 'خدمت نصب')

    def test_category_page_and_menu_do_not_show_it(self):
        self.root.exclude_from_site = True
        self.root.save()
        self.assertEqual(self.client.get(reverse('products:category_detail', args=[self.root.slug])).status_code, 404)
        home = self.client.get(reverse('products:home'))
        self.assertNotContains(home, reverse('products:category_detail', args=[self.root.slug]))

    def test_stale_home_cache_cannot_resurrect_an_excluded_category(self):
        cache.set(home_cache.TOP_CATEGORY_IDS, [self.root.pk, self.shop.pk], 600)
        self.root.exclude_from_site = True
        self.root.save()
        cache.set(home_cache.TOP_CATEGORY_IDS, [self.root.pk, self.shop.pk], 600)     # کش کهنه‌ی ۲۴ساعته
        from products.views import _top_categories
        self.assertEqual([c.pk for c in _top_categories()], [self.shop.pk])

    def test_admin_save_purges_the_products(self):
        product = make_product('خدمت قدیمی', category=self.child)
        keep = make_product('کالای عادی', category=self.shop)
        url = reverse('admin:products_category_change', args=[self.root.pk])
        response = self.client.get(url)
        self.assertContains(response, 'exclude_from_site')
        data = {'name': self.root.name, 'slug': self.root.slug, 'erp_code': self.root.erp_code, 'exclude_from_site': 'on',
                'show_amazing_deals': 'on', 'show_suggested_categories': 'on', 'show_best_sellers': 'on', 'show_frequent': 'on',
                'show_banners': 'on', 'show_blog_posts': 'on', 'short_description': '',
                'banners-TOTAL_FORMS': '0', 'banners-INITIAL_FORMS': '0', 'banners-MIN_NUM_FORMS': '0', 'banners-MAX_NUM_FORMS': '5',
                'mega_menu_banner_url': '', 'mega_menu_banner_alt': ''}
        self.client.post(url, data)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        self.assertTrue(Product.objects.filter(pk=keep.pk).exists())

    def test_bulk_action_flags_selected_categories(self):
        make_product('خدمت', category=self.child)
        self.client.post(reverse('admin:products_category_changelist'), {
            'action': 'exclude_from_site_action', '_selected_action': [self.root.pk]}, follow=True)
        self.root.refresh_from_db()
        self.child.refresh_from_db()
        self.assertTrue(self.root.exclude_from_site and self.child.exclude_from_site)
        self.assertFalse(Product.objects.filter(category=self.child).exists())

    def _item(self, erp, main, side=None, name='کالا'):
        item = {'ErpCode': erp, 'Name': name, 'Code': erp, 'Few': 5, 'SellPrice': 100000, 'IsActive': True,
                'MainGroupErpCode': main, 'MainGroupName': 'گروه', 'SellPrice2': 0, 'SellPrice3': 0, 'SellPrice4': 0,
                'SellPrice5': 0, 'SellPrice6': 0, 'SellPrice7': 0, 'SellPrice8': 0, 'SellPrice9': 0, 'SellPrice10': 0}
        if side:
            item.update({'SideGroupErpCode': side, 'SideGroupName': 'زیرگروه'})
        return item

    def test_sync_skips_excluded_groups_and_purges_existing_products(self):
        existing = make_product('خدمت موجود', category=self.child)
        self.root.exclude_from_site = True
        self.root.save()
        items = [self._item('ERP-NEW-SVC', 'GRP-SVC', 'GRP-SVC-1'), self._item('ERP-NEW-SIDE', 'GRP-OTHER', 'GRP-SVC-1'),
                 self._item('ERP-NEW-OK', 'GRP-SHOP')]
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=3), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': items}):
            sync_products_from_holoo()
        self.assertFalse(Product.objects.filter(erp_code='ERP-NEW-SVC').exists())
        self.assertFalse(Product.objects.filter(erp_code='ERP-NEW-SIDE').exists())     # زیرگروه علامت‌خورده، گروه اصلیِ دیگر
        self.assertTrue(Product.objects.filter(erp_code='ERP-NEW-OK').exists())
        self.assertFalse(Product.objects.filter(pk=existing.pk).exists())
        self.assertFalse(Category.objects.get(erp_code='GRP-SVC').is_active)           # سینک دوباره فعالش نکرد

    def test_data_migration_flags_the_services_category_with_arabic_spelling(self):
        import importlib
        from django.apps import apps
        migration = importlib.import_module('products.migrations.0055_exclude_services_category')
        arabic = Category.objects.create(name='خدمات', slug='svc-arabic')           # همان کلمه؛ ی عربی ندارد، ولی نرمال‌سازی امتحان شود
        arabic_child = Category.objects.create(name='نصب', slug='svc-arabic-child', parent=arabic)
        Category.objects.filter(pk__in=[arabic.pk, self.root.pk]).update(exclude_from_site=False, is_active=True)
        migration.exclude_services_category(apps, None)
        for category in (arabic, arabic_child, self.root, self.child):
            category.refresh_from_db()
            self.assertTrue(category.exclude_from_site and not category.is_active, category.name)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.exclude_from_site)


class CategoryCycleTests(AdminBase):
    """ والدِ یک دسته نباید خودش یا زیردسته‌هایش باشد؛ و داده‌ی خرابِ چرخه‌ای هم پیمایش‌ها را بی‌پایان نکند. """

    def setUp(self):
        super().setUp()
        self.a = Category.objects.create(name='الف', slug='cyc-a')
        self.b = Category.objects.create(name='ب', slug='cyc-b', parent=self.a)
        self.c = Category.objects.create(name='ج', slug='cyc-c', parent=self.b)

    def test_a_category_cannot_be_its_own_parent(self):
        from django.core.exceptions import ValidationError
        self.a.parent = self.a
        with self.assertRaises(ValidationError) as caught:
            self.a.full_clean()
        self.assertIn('parent', caught.exception.message_dict)

    def test_a_category_cannot_move_under_its_own_descendant(self):
        from django.core.exceptions import ValidationError
        self.a.parent = self.c
        with self.assertRaises(ValidationError) as caught:
            self.a.full_clean()
        self.assertIn('parent', caught.exception.message_dict)

    def test_valid_moves_still_pass(self):
        other = Category.objects.create(name='د', slug='cyc-d')
        self.c.parent = other
        self.c.full_clean()
        self.b.parent = None
        self.b.full_clean()

    def test_traversals_terminate_even_if_the_data_already_has_a_cycle(self):
        Category.objects.filter(pk=self.a.pk).update(parent=self.c)                  # حلقه‌ی الف ← ج ← ب ← الف، با نوشتن مستقیم
        self.a.refresh_from_db()
        self.assertCountEqual(self.a.get_descendant_ids(), [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual(len(self.a.get_ancestors()), 3)
        Category.objects.filter(pk=self.a.pk).update(parent=self.a)                  # خود-والد
        self.a.refresh_from_db()
        self.assertEqual(self.a.get_descendant_ids(), [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual([c.pk for c in self.a.get_ancestors()], [self.a.pk])

    def test_the_admin_form_refuses_the_cycle(self):
        url = reverse('admin:products_category_change', args=[self.a.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = {k: v for k, v in response.context['adminform'].form.initial.items() if isinstance(v, (str, int, bool)) and v is not False}
        data.update({'name': 'الف', 'slug': 'cyc-a', 'parent': self.c.pk, 'is_active': 'on'})
        for inline in response.context['inline_admin_formsets']:
            prefix = inline.formset.prefix
            data.update({f'{prefix}-TOTAL_FORMS': 0, f'{prefix}-INITIAL_FORMS': 0, f'{prefix}-MIN_NUM_FORMS': 0, f'{prefix}-MAX_NUM_FORMS': 1000})
        post = self.client.post(url, data)
        self.assertEqual(post.status_code, 200)                                     # دوباره فرم (بدون redirect)
        self.a.refresh_from_db()
        self.assertIsNone(self.a.parent_id)


class SyncImagesAdminTests(TestCase):
    """ همگام‌سازی تصاویر تغییردهنده است: فقط POST + مجوز تغییر محصول، نه GET و نه صرفِ is_staff. """

    def setUp(self):
        self.url = reverse('admin:products_product_sync_images')

    def staff(self, phone, *perms):
        from django.contrib.auth.models import Permission
        user = CustomUser.objects.create_user(phone_number=phone, password='x', is_staff=True)
        for codename in perms:
            user.user_permissions.add(Permission.objects.get(content_type__app_label='products', codename=codename))
        return user

    def test_staff_without_change_permission_gets_403_and_nothing_runs(self):
        self.client.force_login(self.staff('09120000911', 'view_product'))
        with mock.patch('products.admin.sync_product_images') as sync:
            self.assertEqual(self.client.post(self.url).status_code, 403)
            self.assertEqual(self.client.get(self.url).status_code, 403)
        sync.assert_not_called()

    def test_get_never_runs_the_sync_even_for_a_superuser(self):
        self.client.force_login(CustomUser.objects.create_superuser(phone_number='09120000912', password='x'))
        with mock.patch('products.admin.sync_product_images') as sync:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        sync.assert_not_called()

    def test_post_with_permission_runs_it_and_reports_ambiguous_files(self):
        self.client.force_login(self.staff('09120000913', 'change_product', 'view_product'))
        result = {'matched': 1, 'updated': 1, 'pruned': 0, 'unmatched': [], 'ambiguous': ['X-1.jpg']}
        with mock.patch('products.admin.sync_product_images', return_value=result) as sync:
            response = self.client.post(self.url, follow=True)
        sync.assert_called_once()
        self.assertRedirects(response, reverse('admin:products_product_changelist'))
        self.assertContains(response, 'X-1.jpg')

    def test_post_without_csrf_token_is_rejected(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(CustomUser.objects.create_superuser(phone_number='09120000914', password='x'))
        with mock.patch('products.admin.sync_product_images') as sync:
            self.assertEqual(client.post(self.url).status_code, 403)
        sync.assert_not_called()

    def test_the_button_is_a_post_form_with_csrf_and_hidden_without_permission(self):
        changelist = reverse('admin:products_product_changelist')
        self.client.force_login(CustomUser.objects.create_superuser(phone_number='09120000915', password='x'))
        html = self.client.get(changelist).content.decode()
        self.assertIn('csrfmiddlewaretoken', html.split('همگام‌سازی تصاویر')[0].rsplit('<form', 1)[-1])
        self.client.force_login(self.staff('09120000916', 'view_product'))
        self.assertNotIn('همگام‌سازی تصاویر', self.client.get(changelist).content.decode())
