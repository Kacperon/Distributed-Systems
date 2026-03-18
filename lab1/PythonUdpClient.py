import socket

serverIP = "127.0.0.1"
serverPort = 9008

print('PYTHON UDP CLIENT')
client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# wysyłamy czterobajtową wartość 300 w kolejności little‑endian
value = 300
msg_bytes = value.to_bytes(4, byteorder='little')
client.sendto(msg_bytes, (serverIP, serverPort))

data, _ = client.recvfrom(4096)
if len(data) >= 4:
    reply = int.from_bytes(data[:4], byteorder='little', signed=False)
    print('received reply:', reply)