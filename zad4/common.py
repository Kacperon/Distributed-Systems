import os

EXCHANGE = 'space.events'
SERVICES = ('osoby', 'ladunek', 'satelita')
HOST = os.environ.get('RABBITMQ_HOST', 'localhost')
