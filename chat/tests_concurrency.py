"""رقابت واقعی روی یک گفتگو (چند thread و کانکشن جدا): seq بدون حفره/تکرار و idempotency."""
import threading
import uuid

from django.db import connection
from django.test import TransactionTestCase

from chat import cache as chatcache
from chat import conversations as conv
from chat.models import ChatMessage, Conversation
from products.models import SiteSettings


class ConcurrentPostingTests(TransactionTestCase):
    # قرارداد پروژه: همه‌ی TransactionTestCaseها serialized_rollback=True (cart/tests.py:104-106)
    serialized_rollback = True

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        chatcache.reset_breaker()
        SiteSettings.objects.update_or_create(pk=1, defaults={'chat_enabled': True, 'chat_hours_mode': 'always'})
        cache.delete(SiteSettings.CACHE_KEY)
        self.conversation, _ = conv.create_offline_conversation(
            user=None, visitor_hash='c' * 40, name='رقابت', phone='', body='اول',
            client_msg_id=uuid.uuid4(), source_path='/', ip='1.2.3.0')

    def run_threads(self, targets):
        errors = []

        def wrap(fn):
            def inner():
                try:
                    fn()
                except Exception as error:                   # noqa: BLE001 - گزارش در thread اصلی
                    errors.append(error)
                finally:
                    connection.close()
            return inner

        threads = [threading.Thread(target=wrap(t)) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return errors

    def test_parallel_messages_get_gapless_unique_seq(self):
        total = 12

        def post(i):
            def run():
                conversation = Conversation.objects.get(pk=self.conversation.pk)
                conv.post_message(conversation, sender=ChatMessage.SENDER_CUSTOMER, body=f'پیام {i}', client_msg_id=uuid.uuid4())
            return run

        errors = self.run_threads([post(i) for i in range(total)])
        self.assertEqual(errors, [])
        seqs = list(ChatMessage.objects.filter(conversation=self.conversation).order_by('seq').values_list('seq', flat=True))
        self.assertEqual(seqs, list(range(1, total + 2)))                       # ۱ تا N+1 بدون حفره و تکرار
        self.assertEqual(Conversation.objects.get(pk=self.conversation.pk).last_message_seq, total + 1)

    def test_same_client_msg_id_is_stored_once(self):
        cid = uuid.uuid4()
        results = []

        def run():
            conversation = Conversation.objects.get(pk=self.conversation.pk)
            _, created = conv.post_message(conversation, sender=ChatMessage.SENDER_CUSTOMER, body='تکراری', client_msg_id=cid)
            results.append(created)

        errors = self.run_threads([run] * 6)
        self.assertEqual(errors, [])
        self.assertEqual(ChatMessage.objects.filter(conversation=self.conversation, client_msg_id=cid).count(), 1)
        self.assertEqual(results.count(True), 1)
