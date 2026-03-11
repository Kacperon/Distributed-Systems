import socket
import threading
import struct

PORT = 12345
MCAST_GRP = '224.1.1.1'

clients = {}       
udp_clients = {} 


def broadcast_tcp(msg, sender_nick=None):
    for nick, sock in list(clients.items()):
        if nick != sender_nick:
            sock.sendall(msg.encode())

def handle_client(conn, addr):
    conn.sendall(b"Nick: ")
    nick = conn.recv(1024).decode().strip()
    clients[nick] = conn
    broadcast_tcp(f"*** {nick} joined\n")

    while True:
        data = conn.recv(1024)
        if not data:
            break
        broadcast_tcp(f"{nick}: {data.decode()}\n", sender_nick=nick)

    del clients[nick]
    conn.close()
    broadcast_tcp(f"*** {nick} left\n")


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", PORT))

    while True:
        data, addr = sock.recvfrom(65535)
        txt = data.decode()
        sender = txt.split("\n", 1)[0]
        udp_clients[sender] = addr
        for nick, a in list(udp_clients.items()):
            if nick != sender:
                sock.sendto(data, a)



def main():
    threading.Thread(target=udp_listener, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", PORT))
    srv.listen()
    print(f"Server listening on port {PORT}")

    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    main()
