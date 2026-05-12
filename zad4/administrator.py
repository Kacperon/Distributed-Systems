import json
import threading

import pika

from common import EXCHANGE, HOST


TARGETS = {
    'agencies': 'broadcast.agencies',
    'carriers': 'broadcast.carriers',
    'all': 'broadcast.all',
}


def consumer():
    conn = pika.BlockingConnection(pika.ConnectionParameters(host=HOST))
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type='topic')
    q = ch.queue_declare(queue='', exclusive=True).method.queue
    ch.queue_bind(q, EXCHANGE, 'order.*')
    ch.queue_bind(q, EXCHANGE, 'confirm.*')
    ch.queue_bind(q, EXCHANGE, 'broadcast.*')

    def cb(ch_, method, props, body):
        msg = json.loads(body)
        print(f'\n[SPY {method.routing_key}] {msg}')
        print('admin> ', end='', flush=True)

    ch.basic_consume(queue=q, on_message_callback=cb, auto_ack=True)
    ch.start_consuming()


def main():
    threading.Thread(target=consumer, daemon=True).start()

    conn = pika.BlockingConnection(pika.ConnectionParameters(host=HOST, heartbeat=0))
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type='topic')

    print('Administrator ready. Commands: agencies <text> | carriers <text> | all <text> | quit')
    while True:
        try:
            line = input('admin> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == 'quit':
            break
        parts = line.split(maxsplit=1)
        if len(parts) < 2 or parts[0] not in TARGETS:
            print('usage: agencies <text> | carriers <text> | all <text> | quit')
            continue
        target, text = parts
        ch.basic_publish(
            exchange=EXCHANGE,
            routing_key=TARGETS[target],
            body=json.dumps({'text': text}),
        )
        print(f'  sent to {target}')

    conn.close()


if __name__ == '__main__':
    main()
