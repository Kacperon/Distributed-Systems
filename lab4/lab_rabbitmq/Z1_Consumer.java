import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Connection;
import com.rabbitmq.client.ConnectionFactory;
import com.rabbitmq.client.Consumer;
import com.rabbitmq.client.DefaultConsumer;
import com.rabbitmq.client.Envelope;

import java.io.IOException;

public class Z1_Consumer {

    public static void main(String[] argv) throws Exception {

        // arg 1: tryb ACK
        //   "auto"    -> autoAck=true  (potwierdzenie po OTRZYMANIU wiadomości)
        //   "manual"  -> autoAck=false + basicAck po przetworzeniu
        //   "none"    -> autoAck=false, brak basicAck w ogóle
        // arg 2: QoS
        //   "qos"     -> channel.basicQos(1) -> load balancing
        //   "noqos"   -> brak QoS -> round-robin
        String ackMode = (argv.length > 0) ? argv[0] : "manual";
        String qosMode = (argv.length > 1) ? argv[1] : "noqos";

        System.out.println("Z1 CONSUMER (ackMode=" + ackMode + ", qos=" + qosMode + ")");

        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost("localhost");
        Connection connection = factory.newConnection();
        final Channel channel = connection.createChannel();

        String QUEUE_NAME = "queue1";
        channel.queueDeclare(QUEUE_NAME, false, false, false, null);

        if ("qos".equals(qosMode)) {
            channel.basicQos(1);
        }

        final boolean manualAck = "manual".equals(ackMode);

        Consumer consumer = new DefaultConsumer(channel) {
            @Override
            public void handleDelivery(String consumerTag, Envelope envelope,
                                       AMQP.BasicProperties properties, byte[] body) throws IOException {
                String message = new String(body, "UTF-8");
                long t0 = System.currentTimeMillis();
                System.out.println("Received: " + message + "  [start processing]");

                try {
                    int timeToSleep = Integer.parseInt(message.trim());
                    Thread.sleep(timeToSleep * 1000L);
                } catch (NumberFormatException e) {
                    System.out.println("(not a number, processing 'instant')");
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }

                long elapsedMs = System.currentTimeMillis() - t0;
                System.out.printf("Done: %s  (took %.2fs)%n", message, elapsedMs / 1000.0);

                if (manualAck) {
                    channel.basicAck(envelope.getDeliveryTag(), false);
                    System.out.println("  -> ACK sent");
                }
                // "auto"  -> broker już potwierdził za nas, nic nie robimy
                // "none"  -> celowo nie ACK-ujemy (patrz pytanie 2)
            }
        };

        boolean autoAck = "auto".equals(ackMode);
        System.out.println("Waiting for messages...  (autoAck=" + autoAck + ")");
        channel.basicConsume(QUEUE_NAME, autoAck, consumer);

    }
}
