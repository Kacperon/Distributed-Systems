import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

public class JavaUdpServer {

    public static void main(String args[])
    {
        System.out.println("JAVA UDP SERVER");
        DatagramSocket socket = null;
        int portNumber = 9008;

        try{
            socket = new DatagramSocket(portNumber);
            byte[] receiveBuffer = new byte[1024];

            while(true) {
                Arrays.fill(receiveBuffer, (byte)0);
                DatagramPacket receivePacket =
                    new DatagramPacket(receiveBuffer, receiveBuffer.length);
                socket.receive(receivePacket);

                byte[] data = receivePacket.getData();
                int value = 0;
                value = ByteBuffer.wrap(data, 0, 4)
                                      .order(ByteOrder.LITTLE_ENDIAN)
                                      .getInt();

                System.out.println("received msg: " + value);


                int replyValue = value + 1;
                byte[] sendBuffer =
                    ByteBuffer.allocate(4)
                              .order(ByteOrder.LITTLE_ENDIAN)
                              .putInt(replyValue)
                              .array();

                InetAddress clientAddr = receivePacket.getAddress();
                int clientPort = receivePacket.getPort();
                DatagramPacket sendPacket =
                    new DatagramPacket(sendBuffer, sendBuffer.length,
                                       clientAddr, clientPort);
                socket.send(sendPacket);
            }
        }
        catch(Exception e){
            e.printStackTrace();
        }
        finally {
            if (socket != null) {
                socket.close();
            }
        }
    }
}