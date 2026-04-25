import com.rabbitmq.client.BuiltinExchangeType;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Connection;
import com.rabbitmq.client.ConnectionFactory;

import java.io.BufferedReader;
import java.io.InputStreamReader;

public class Z2b_Producer {

    public static void main(String[] argv) throws Exception {

        // arg 1: "direct" albo "topic"
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

        System.out.println("Z2b PRODUCER (" + type + ", exchange=" + EXCHANGE_NAME + ")");

        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost("localhost");
        Connection connection = factory.newConnection();
        Channel channel = connection.createChannel();

        channel.exchangeDeclare(EXCHANGE_NAME, exchangeType);

        BufferedReader br = new BufferedReader(new InputStreamReader(System.in));
        System.out.println("Format: <routingKey> <message>");
        System.out.println("  direct: np.  error  Critical failure");
        System.out.println("  topic:  np.  eu.news.sport  Mecz 2-1");
        System.out.println("  'exit' aby zakonczyc");

        while (true) {
            System.out.print("> ");
            String line = br.readLine();
            if (line == null || "exit".equals(line)) break;
            line = line.trim();
            if (line.isEmpty()) continue;

            int sp = line.indexOf(' ');
            String key;
            String message;
            if (sp < 0) {
                key = line;
                message = "(empty)";
            } else {
                key = line.substring(0, sp);
                message = line.substring(sp + 1);
            }

            channel.basicPublish(EXCHANGE_NAME, key, null, message.getBytes("UTF-8"));
            System.out.println("Sent [key=" + key + "]: " + message);
        }

        channel.close();
        connection.close();
    }
}
