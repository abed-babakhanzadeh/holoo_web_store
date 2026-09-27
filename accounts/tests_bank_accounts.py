"""
تست‌های مدل UserBankAccount: قاعده‌ی «حساب پیش‌فرض» (هم‌الگوی Address)، اعتبارسنجی شماره کارت/شبا،
و سناریوی هم‌زمانی روی همان کاربر.
"""

import threading

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase

from accounts.models import CustomUser, UserBankAccount


class BankAccountTestMixin:
    def make_user(self, phone='09130001001'):
        return CustomUser.objects.create_user(phone_number=phone)

    def make_account(self, user, **overrides):
        data = dict(
            user=user, account_holder_first_name='علی', account_holder_last_name='رضایی',
            card_number='6037991234567890',
        )
        data.update(overrides)
        return UserBankAccount.objects.create(**data)

    @staticmethod
    def defaults_of(user):
        return list(UserBankAccount.objects.filter(user=user, is_default=True))


class DefaultBankAccountRuleTests(BankAccountTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()

    def test_user_without_accounts_has_no_default(self):
        self.assertEqual(self.defaults_of(self.user), [])

    def test_first_account_becomes_default_even_if_not_requested(self):
        first = self.make_account(self.user, is_default=False)
        self.assertTrue(first.is_default)

    def test_second_account_is_not_default_unless_requested(self):
        first = self.make_account(self.user)
        second = self.make_account(self.user, card_number='6219861234567890')
        self.assertFalse(second.is_default)
        self.assertEqual(self.defaults_of(self.user), [first])

    def test_creating_with_is_default_moves_the_default(self):
        self.make_account(self.user)
        newer = self.make_account(self.user, card_number='6219861234567890', is_default=True)
        self.assertEqual(self.defaults_of(self.user), [newer])

    def test_set_default_switches_and_keeps_exactly_one(self):
        first = self.make_account(self.user)
        second = self.make_account(self.user, card_number='6219861234567890')
        second.set_default()
        self.assertEqual(self.defaults_of(self.user), [second])
        first.refresh_from_db()
        first.set_default()
        self.assertEqual(self.defaults_of(self.user), [first])

    def test_cannot_unset_the_only_default_directly(self):
        first = self.make_account(self.user)
        self.make_account(self.user, card_number='6219861234567890')
        first.is_default = False
        first.save()
        first.refresh_from_db()
        self.assertTrue(first.is_default)
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_deleting_default_promotes_the_newest_remaining(self):
        first = self.make_account(self.user)
        second = self.make_account(self.user, card_number='6219861234567890')
        third = self.make_account(self.user, card_number='5022291234567890')
        first.delete()
        self.assertEqual(self.defaults_of(self.user), [third])
        third.delete()
        self.assertEqual(self.defaults_of(self.user), [second])

    def test_deleting_non_default_keeps_default_and_deleting_last_leaves_none(self):
        first = self.make_account(self.user)
        second = self.make_account(self.user, card_number='6219861234567890')
        second.delete()
        self.assertEqual(self.defaults_of(self.user), [first])
        first.delete()
        self.assertEqual(self.defaults_of(self.user), [])
        self.assertEqual(UserBankAccount.objects.filter(user=self.user).count(), 0)

    def test_other_users_defaults_are_independent(self):
        other = self.make_user('09130001002')
        mine = self.make_account(self.user)
        theirs = self.make_account(other)
        self.make_account(self.user, card_number='6219861234567890').set_default()
        theirs.refresh_from_db()
        self.assertTrue(theirs.is_default)
        self.assertNotEqual(self.defaults_of(self.user), [mine])

    def test_deleting_the_user_cascades_without_error(self):
        self.make_account(self.user)
        self.make_account(self.user, card_number='6219861234567890')
        self.user.delete()
        self.assertEqual(UserBankAccount.objects.count(), 0)

    def test_queryset_delete_of_default_promotes_a_survivor(self):
        a = self.make_account(self.user)
        b = self.make_account(self.user, card_number='6219861234567890')
        c = self.make_account(self.user, card_number='5022291234567890')
        UserBankAccount.objects.filter(pk=a.pk).delete()
        self.assertEqual(self.defaults_of(self.user), [c])
        self.assertEqual(UserBankAccount.objects.filter(user=self.user).count(), 2)


class BankAccountValidationTests(BankAccountTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()

    def test_requires_first_and_last_name(self):
        for field in ('account_holder_first_name', 'account_holder_last_name'):
            with self.subTest(field=field), self.assertRaises(ValidationError) as ctx:
                self.make_account(self.user, **{field: ''})
            self.assertIn(field, ctx.exception.message_dict)

    def test_card_number_must_be_16_digits(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_account(self.user, card_number='123')
        self.assertIn('card_number', ctx.exception.message_dict)

    def test_iban_is_normalized_with_ir_prefix(self):
        account = self.make_account(self.user, card_number='', iban='620570123456789012345678')
        self.assertEqual(account.iban, 'IR620570123456789012345678')
        account2 = self.make_account(
            self.user, card_number='', iban='IR620570123456789012345678', account_holder_first_name='ب',
        )
        self.assertEqual(account2.iban, 'IR620570123456789012345678')

    def test_requires_at_least_card_or_iban(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_account(self.user, card_number='')
        self.assertIn('card_number', ctx.exception.message_dict)

    def test_masked_display_and_str(self):
        account = self.make_account(self.user)
        self.assertIn('7890', account.masked_display)
        self.assertIn('7890', str(account))


class BankAccountConcurrencyTests(BankAccountTestMixin, TransactionTestCase):
    """ درخواست‌های هم‌زمان واقعی (ترد + اتصال جدا) نباید دو پیش‌فرض بسازند """

    serialized_rollback = True
    WORKERS = 6

    def setUp(self):
        self.user = self.make_user('09130002001')

    def run_concurrently(self, jobs):
        barrier = threading.Barrier(len(jobs))
        errors = []

        def wrap(job):
            def runner():
                try:
                    barrier.wait(timeout=30)
                    job()
                except BaseException as exc:      # noqa: BLE001
                    errors.append(repr(exc))
                finally:
                    connection.close()
            return runner

        threads = [threading.Thread(target=wrap(job)) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        return errors

    def test_concurrent_first_accounts_create_exactly_one_default(self):
        jobs = [
            lambda i=i: self.make_account(self.user, card_number=f'60379912345{i:05d}')
            for i in range(self.WORKERS)
        ]
        errors = self.run_concurrently(jobs)
        self.assertEqual(errors, [])
        self.assertEqual(UserBankAccount.objects.filter(user=self.user).count(), self.WORKERS)
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_concurrent_set_default_leaves_exactly_one(self):
        accounts = [
            self.make_account(self.user, card_number=f'60379912345{i:05d}')
            for i in range(self.WORKERS)
        ]
        errors = self.run_concurrently(
            [lambda a=a: UserBankAccount.objects.get(pk=a.pk).set_default() for a in accounts],
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(self.defaults_of(self.user)), 1)
