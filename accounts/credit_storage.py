from services.private_storage import PrivateMediaStorage


class ChequeCreditDocStorage(PrivateMediaStorage):
    """ مدارک درخواست خرید چکی (دسته‌چک، کارت ملی، ...): MEDIA_ROOT/cheque_credit_docs/؛ بدون نشانی عمومی (فقط صاحب درخواست و ادمین) """
    subdir = 'cheque_credit_docs'


cheque_credit_doc_storage = ChequeCreditDocStorage()
