import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Connection;
import com.rabbitmq.client.ConnectionFactory;

import java.io.BufferedReader;
import java.io.InputStreamReader;

public class Z1_Producer {

    public static void main(String[] argv) throws Exception {

        System.out.println("Z1 PRODUCER");

        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost("localhost");
        Connection connection = factory.newConnection();
        Channel channel = connection.createChannel();

        String QUEUE_NAME = "queue1";
        channel.queueDeclare(QUEUE_NAME, false, false, false, null);

        BufferedReader br = new BufferedReader(new InputStreamReader(System.in));
        System.out.println("Commands:");
        System.out.println("  <number>          -> wyslij jedna wiadomosc (liczba sek. pracy)");
        System.out.println("  burst 1,5,1,5...  -> wyslij cala serie naraz (oddzielone przecinkami)");
        System.out.println("  zad1b             -> skrot dla 1,5,1,5,1,5,1,5,1,5");
        System.out.println("  exit              -> koniec");

        while (true) {
            System.out.print("> ");
            String line = br.readLine();
            if (line == null || "exit".equals(line)) break;
            line = line.trim();
            if (line.isEmpty()) continue;

            String[] values;
            if ("zad1b".equals(line)) {
                values = "1,5,1,5,1,5,1,5,1,5".split(",");
            } else if (line.startsWith("burst ")) {
                values = line.substring(6).split(",");
            } else {
                values = new String[] { line };
            }

            for (String v : values) {
                String msg = v.trim();
                channel.basicPublish("", QUEUE_NAME, null, msg.getBytes("UTF-8"));
                System.out.println("Sent: " + msg);
            }
        }

        channel.close();
        connection.close();
    }
}
