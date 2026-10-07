from . import statemachine as sm


def iso(value):
    return value.isoformat() if value else None


def operator_label(message):
    operator = message.operator
    if operator is None:
        return 'کارشناس'
    return (operator.first_name or '').strip() or 'کارشناس'


def message_for_customer(message):
    """ هرگز یادداشت داخلی را به این تابع ندهید (فیلتر در messages_after/is_internal_note) """
    return {
        'seq': message.seq, 'sender': message.sender_type, 'body': message.body, 'at': iso(message.created_at),
        'operator': operator_label(message) if message.sender_type == 'operator' else '',
    }


def message_for_operator(message):
    data = message_for_customer(message)
    data['note'] = message.is_internal_note
    return data


def conversation_for_customer(conversation):
    return {
        'id': str(conversation.public_id), 'status': conversation.status, 'channel': conversation.channel_origin, 'closed': conversation.status == sm.CLOSED,
        'unread': conversation.unread_for_customer, 'last_seq': conversation.last_message_seq,
        'last_read_seq': conversation.last_read_seq_by_customer,
    }
