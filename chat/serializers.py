from django.urls import reverse

from . import statemachine as sm


def iso(value):
    return value.isoformat() if value else None


def operator_label(message):
    operator = message.operator
    if operator is None:
        return 'کارشناس'
    return (operator.first_name or '').strip() or 'کارشناس'


def customer_file_url(public_id):
    def build(attachment):
        return reverse('chat:file', args=[public_id, attachment.public_id])
    return build


def operator_file_url(attachment):
    return reverse('admin:chat_console_file', args=[attachment.public_id])


def _base(message):
    return {
        'seq': message.seq, 'sender': message.sender_type, 'body': message.body, 'at': iso(message.created_at),
        'operator': operator_label(message) if message.sender_type == 'operator' else '',
    }


def message_for_customer(message, public_id=None):
    """ هرگز یادداشت داخلی را به این تابع ندهید (فیلتر در messages_after/is_internal_note) """
    from . import attachments

    data = _base(message)
    data['attachments'] = attachments.payload(message, customer_file_url(public_id or message.conversation.public_id))
    return data


def message_for_operator(message):
    from . import attachments

    data = _base(message)
    data['note'] = message.is_internal_note
    data['attachments'] = attachments.payload(message, operator_file_url)
    return data


def conversation_for_customer(conversation):
    return {
        'id': str(conversation.public_id), 'status': conversation.status, 'channel': conversation.channel_origin,
        'closed': conversation.status == sm.CLOSED,
        'unread': conversation.unread_for_customer, 'last_seq': conversation.last_message_seq,
        'last_read_seq': conversation.last_read_seq_by_customer,
    }
