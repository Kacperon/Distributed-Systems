import os

EXCHANGE = 'space.events'

SERVICES = ('osoby', 'ladunek', 'satelita')


def host():
    return os.environ.get('RABBITMQ_HOST', 'localhost')
