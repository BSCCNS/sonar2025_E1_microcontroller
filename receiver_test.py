import socket

# Create a UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Bind to port 8080 on all interfaces
sock.bind(('0.0.0.0', 8080))

print("Listening on UDP port 8080...")

while True:
    data, addr = sock.recvfrom(4096)  # buffer size 1024 bytes
    print(f"Received from {addr}: {data.decode(errors='ignore')}")