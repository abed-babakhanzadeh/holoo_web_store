from services.private_storage import PrivateMediaStorage


class ChequeImageStorage(PrivateMediaStorage):
    """ تصاویر چک: MEDIA_ROOT/cheque_images/؛ بدون نشانی عمومی (فقط صاحب سفارش و ادمین از ویوی کنترل‌دسترسی می‌بینند) """
    subdir = 'cheque_images'


cheque_image_storage = ChequeImageStorage()
