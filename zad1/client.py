import socket
import threading
import struct

PORT = 12345
MCAST_GRP = '224.1.1.1'
nick = ""

def tcp_reader(sock):
    while True:
        data = sock.recv(1024)
        if not data:
            break
        print(data.decode(), end='')

def udp_reader(sock):
    while True:
        data, _ = sock.recvfrom(65535)
        txt = data.decode()

        sender = txt.split("\n", 1)[0]
        if sender == nick:
            continue
        payload = txt.split("\n", 1)[1] if "\n" in txt else txt
        print(f"[UDP from {sender}]\n{payload}")

def mcast_reader(sock):
    while True:
        data, _ = sock.recvfrom(65535)
        txt = data.decode()
        sender = txt.split("\n", 1)[0]
        if sender == nick:
            continue
        payload = txt.split("\n", 1)[1] if "\n" in txt else txt
        print(f"[MCAST from {sender}] {payload}")


def main():
    global nick
    nick = input("Nick: ")

    # TCP
    tsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tsock.connect(('localhost', PORT))
    tsock.recv(1024)
    tsock.sendall((nick + '\n').encode())
    threading.Thread(target=tcp_reader, args=(tsock,), daemon=True).start()

    # UDP (ephemeral port — each client gets unique addr)
    usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    usock.bind(("", 0))
    threading.Thread(target=udp_reader, args=(usock,), daemon=True).start()

    # Multicast
    msock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    msock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    msock.bind(("", PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    msock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    threading.Thread(target=mcast_reader, args=(msock,), daemon=True).start()

    # input loop
    while True:
        line = input()
        if line == 'U':
            art = open('art.txt').read()
            # prepend nick so server and receivers know who sent it
            usock.sendto(f"{nick}\n{art}".encode(), ('localhost', PORT))
        elif line == 'M':
            msg = input('mcast> ')
            msock.sendto(f"{nick}\n{msg}".encode(), (MCAST_GRP, PORT))
        else:
            tsock.sendall(line.encode())

if __name__ == '__main__':
    main()
