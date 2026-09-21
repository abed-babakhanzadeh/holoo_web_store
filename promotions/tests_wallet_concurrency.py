"""
تست هم‌زمانی چندنخی «دریافت کد» روی دیتابیس واقعی (SQL Server، تراکنش‌های واقعی).

تضمین‌ها: با سقف دریافت‌کننده K دقیقاً K کاربر می‌گیرند؛ چند کلیک هم‌زمان یک کاربر فقط یک ردیف می‌سازد؛ متن کد فقط به
دریافت‌کننده‌ی واقعی می‌رسد؛ دریافت‌های هم‌زمانِ چند کد توسط یک کاربر deadlock نمی‌سازد.
"""

from django.test import Client
from django.urls import reverse

from accounts.models import CustomUser
from orders.tests_coupon_concurrency import CouponConcurrencyBase

from . import wallet
from .models import UserCoupon
from .testing import make_coupon


class ClaimRaceBase(CouponConcurrencyBase):
    def buyers(self, count, start=0):
        return [CustomUser.objects.create_user(phone_number=f'0912066{i:04d}', price_level=1) for i in range(start, start + count)]

    def claimable(self, code, **fields):
        return make_coupon(code, is_claimable=True, title='کمپین هم‌زمانی', **fields)


class ClaimLimitRaceTests(ClaimRaceBase):
    def test_claim_limit_three_of_ten_users(self):
        coupon = self.claimable('CLAIMRACE', claim_limit=3)
        users = self.buyers(10)
        results = self.run_threads([lambda u=u: wallet.claim(u, coupon.pk) for u in users])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(sorted(r.status for r in results), ['claimed'] * 3 + ['full'] * 7)
        self.assertEqual(UserCoupon.objects.filter(coupon=coupon).count(), 3)
        winners = {r.code for r in results if r.ok}
        self.assertEqual(winners, {'CLAIMRACE'})
        # بازنده‌ها هیچ کدی نگرفتند و متن کد هم به آن‌ها نرسید
        self.assertTrue(all(r.code == '' for r in results if not r.ok))

    def test_one_user_clicking_many_times_at_once_gets_one_row(self):
        coupon = self.claimable('DOUBLECLICK')
        user = self.buyers(1)[0]
        results = self.run_threads([lambda: wallet.claim(user, coupon.pk) for _ in range(8)])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(sorted(r.status for r in results), ['already'] * 7 + ['claimed'])
        self.assertEqual(UserCoupon.objects.filter(user=user, coupon=coupon).count(), 1)

    def test_a_full_coupon_never_lets_late_comers_in(self):
        coupon = self.claimable('TINY', claim_limit=1)
        users = self.buyers(12)
        results = self.run_threads([lambda u=u: wallet.claim(u, coupon.pk) for u in users])
        self.assertEqual(sum(1 for r in results if r.status == 'claimed'), 1)
        self.assertEqual(UserCoupon.objects.filter(coupon=coupon).count(), 1)

    def test_one_user_claiming_several_coupons_concurrently_never_deadlocks(self):
        coupons_ = [self.claimable(f'MULTI{i}') for i in range(4)]
        user = self.buyers(1)[0]
        jobs = [lambda c=c: wallet.claim(user, c.pk) for c in coupons_ for _ in range(2)]
        results = self.run_threads(jobs)
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(UserCoupon.objects.filter(user=user).count(), 4)
        self.assertEqual(sorted(r.status for r in results), ['already'] * 4 + ['claimed'] * 4)


class ClaimEndpointRaceTests(ClaimRaceBase):
    """ از مسیر واقعی HTTP/HTMX: کد فقط در پاسخِ دریافت‌کنندگان می‌آید """

    def claim_job(self, user, coupon_pk):
        def job():
            client = Client()
            client.force_login(user)
            return client.post(reverse('promotions:claim', args=[coupon_pk]), HTTP_HX_REQUEST='true')
        return job

    def test_only_the_winners_responses_contain_the_code(self):
        coupon = self.claimable('HTTPRACE', claim_limit=2)
        users = self.buyers(6, start=100)
        responses = self.run_threads([self.claim_job(u, coupon.pk) for u in users])
        self.assertEqual([r for r in responses if isinstance(r, BaseException)], [])
        self.assertTrue(all(r.status_code == 200 for r in responses))
        with_code = [r for r in responses if 'id="claimed-code"' in r.content.decode()]
        self.assertEqual(len(with_code), 2)
        for response in responses:
            html = response.content.decode()
            if response not in with_code:
                self.assertNotIn('HTTPRACE', html)
                self.assertIn(wallet.CLAIM_MESSAGES['full'], html)
        self.assertEqual(UserCoupon.objects.filter(coupon=coupon).count(), 2)

    def test_claiming_and_the_used_cap_together(self):
        """ سقف مصرف ۱ و پیش‌تر پر شده؛ هیچ‌کس نباید بتواند بعدش دریافت کند، حتی هم‌زمان """
        from django.utils import timezone
        from datetime import timedelta
        from .models import CouponRedemption
        from orders.models import Order
        coupon = self.claimable('CAPFULL', total_limit=1, per_user_limit=None)
        holder = self.buyers(1, start=200)[0]
        order = Order.objects.create(user=holder, first_name='الف', last_name='ب', phone='09120000000', address='x', total_price=1)
        CouponRedemption.objects.create(coupon=coupon, user=holder, order_id=order.pk, code='CAPFULL', status='redeemed',
                                        discount_amount=1, expires_at=timezone.now() + timedelta(minutes=5))
        users = self.buyers(5, start=210)
        results = self.run_threads([lambda u=u: wallet.claim(u, coupon.pk) for u in users])
        self.assertEqual({r.status for r in results}, {'full'})
        self.assertFalse(UserCoupon.objects.filter(coupon=coupon).exists())
