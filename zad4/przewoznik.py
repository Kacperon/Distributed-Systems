import json
import sys

import pika

from common import EXCHANGE, SERVICES, host


def main():
    if len(sys.argv) != 4:
        sys.exit(f'usage: przewoznik.py <id> <service1> <service2>  (services: {", ".join(SERVICES)})')
    carrier_id = sys.argv[1]
    services = sys.argv[2:]
    if len(set(services)) != 2 or any(s not in SERVICES for s in services):
        sys.exit(f'must give 2 distinct services from: {", ".join(SERVICES)}')

    conn = pika.BlockingConnection(pika.ConnectionParameters(host=host()))
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type='topic')

    for s in services:
        q = f'service.{s}'
        ch.queue_declare(queue=q)
        ch.queue_bind(q, EXCHANGE, f'order.{s}')

    own = f'carrier.{carrier_id}'
    ch.queue_declare(queue=own, exclusive=True)
    ch.queue_bind(own, EXCHANGE, 'broadcast.carriers')
    ch.queue_bind(own, EXCHANGE, 'broadcast.all')

    ch.basic_qos(prefetch_count=1)

    def order_cb(ch_, method, props, body):
        msg = json.loads(body)
        service = method.routing_key.split('.', 1)[1]
        print(f"[ORDER] {msg['agency']} #{msg['order_id']} {service}")
        confirm = {
            'agency': msg['agency'],
            'order_id': msg['order_id'],
            'service': service,
            'carrier': carrier_id,
        }
        ch_.basic_publish(
            exchange=EXCHANGE,
            routing_key=f"confirm.{msg['agency']}",
            body=json.dumps(confirm),
        )
        ch_.basic_ack(method.delivery_tag)
        print(f"  confirmed {msg['agency']} #{msg['order_id']}")

    def broadcast_cb(ch_, method, props, body):
        msg = json.loads(body)
        print(f"[ADMIN -> {method.routing_key}] {msg.get('text', '')}")

    for s in services:
        ch.basic_consume(queue=f'service.{s}', on_message_callback=order_cb)
    ch.basic_consume(queue=own, on_message_callback=broadcast_cb, auto_ack=True)

    print(f'Przewoznik {carrier_id} ready (services: {", ".join(services)})')
    try:
        ch.start_consuming()
    except KeyboardInterrupt:
        ch.stop_consuming()
    conn.close()


if __name__ == '__main__':
    main()
