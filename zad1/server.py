import socket
import threading
import struct

PORT = 12346
MCAST_GRP = '224.1.1.1'

clients = {}       
# Track UDP peers by address, not nick, to avoid collisions when nicks repeat.
udp_clients = set()


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
        print(f"[UDP recv] {addr} bytes={len(data)}")
        udp_clients.add(addr)
        # Fan-out to every known peer except the sender.
        for a in list(udp_clients):
            if a != addr:
                print(f"[UDP send] -> {a} bytes={len(data)}")
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
