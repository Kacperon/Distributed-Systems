import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.BuiltinExchangeType;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Connection;
import com.rabbitmq.client.ConnectionFactory;
import com.rabbitmq.client.Consumer;
import com.rabbitmq.client.DefaultConsumer;
import com.rabbitmq.client.Envelope;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

public class Z2b_Consumer {

    public static void main(String[] argv) throws Exception {

        // arg 1: "direct" albo "topic"
        // arg 2..: klucze bindingu (w Direct - wartosci dokladne, w Topic - wzorce z * i #)
        //          jesli brak, pyta z konsoli
        String type = (argv.length > 0) ? argv[0] : "direct";

        BuiltinExchangeType exchangeType;
        String EXCHANGE_NAME;
        if ("topic".equals(type)) {
            exchangeType = BuiltinExchangeType.TOPIC;
            EXCHANGE_NAME = "exchange_topic";
        } else {
            exchangeType = BuiltinExchangeType.DIRECT;
            EXCHANGE_NAME = "exchange_direct";
        }

        List<String> keys = new ArrayList<>();
        for (int i = 1; i < argv.length; i++) keys.add(argv[i]);
        if (keys.isEmpty()) {
            BufferedReader br = new BufferedReader(new InputStreamReader(System.in));
            System.out.println("Podaj klucze bindingu oddzielone spacja (np. 'error warning'");
            System.out.println("albo dla topic: '*.news.* eu.#'):");
            System.out.print("> ");
            String line = br.readLine();
            if (line != null) {
                for (String k : line.trim().split("\\s+")) {
                    if (!k.isEmpty()) keys.add(k);
                }
            }
        }
        if (keys.isEmpty()) {
            System.err.println("Brak kluczy bindingu - koncze.");
            System.exit(1);
        }

        System.out.println("Z2b CONSUMER (" + type + ", exchange=" + EXCHANGE_NAME + ", keys=" + keys + ")");

        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost("localhost");
        Connection connection = factory.newConnection();
        Channel channel = connection.createChannel();

        channel.exchangeDeclare(EXCHANGE_NAME, exchangeType);

        String queueName = channel.queueDeclare().getQueue();
        for (String key : keys) {
            channel.queueBind(queueName, EXCHANGE_NAME, key);
        }
        System.out.println("created queue: " + queueName);

        Consumer consumer = new DefaultConsumer(channel) {
            @Override
            public void handleDelivery(String consumerTag, Envelope envelope,
                                       AMQP.BasicProperties properties, byte[] body) throws IOException {
                String message = new String(body, "UTF-8");
                System.out.println("Received [key=" + envelope.getRoutingKey() + "]: " + message);
            }
        };

        System.out.println("Waiting for messages...");
        channel.basicConsume(queueName, true, consumer);
    }
}
