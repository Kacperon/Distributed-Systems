import json
import sys
import threading

import pika

from common import EXCHANGE, SERVICES, HOST


def consumer(name):
    conn = pika.BlockingConnection(pika.ConnectionParameters(host=HOST))
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type='topic')
    queue = f'agency.{name}'
    ch.queue_declare(queue=queue, exclusive=True)
    ch.queue_bind(queue, EXCHANGE, f'confirm.{name}')
    ch.queue_bind(queue, EXCHANGE, 'broadcast.agencies')
    ch.queue_bind(queue, EXCHANGE, 'broadcast.all')

    def cb(ch_, method, props, body):
        rk = method.routing_key
        msg = json.loads(body)
        if rk.startswith('confirm.'):
            print(f"\n[CONFIRM] order #{msg['order_id']} ({msg['service']}) done by carrier {msg['carrier']}")
        else:
            print(f"\n[ADMIN -> {rk}] {msg['text']}")
        print(f'{name}> ', end='', flush=True)

    ch.basic_consume(queue=queue, on_message_callback=cb, auto_ack=True)
    ch.start_consuming()


def main():
    if len(sys.argv) != 2:
        sys.exit('usage: agencja.py <name>')
    name = sys.argv[1]

    threading.Thread(target=consumer, args=(name,), daemon=True).start()

    conn = pika.BlockingConnection(pika.ConnectionParameters(host=HOST, heartbeat=0))
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type='topic')

    print(f'Agencja {name} ready. Commands: {", ".join(SERVICES)} | quit')
    counter = 0
    while True:
        try:
            cmd = input(f'{name}> ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if not cmd:
            continue
        if cmd == 'quit':
            break
        if cmd not in SERVICES:
            print(f'unknown. use: {", ".join(SERVICES)} | quit')
            continue
        counter += 1
        msg = {'agency': name, 'order_id': counter, 'service': cmd}
        ch.basic_publish(exchange=EXCHANGE, routing_key=f'order.{cmd}', body=json.dumps(msg))
        print(f'  sent order #{counter} for {cmd}')

    conn.close()


if __name__ == '__main__':
    main()
