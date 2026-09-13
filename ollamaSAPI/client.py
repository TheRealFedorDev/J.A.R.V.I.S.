import json
import socket

# Просто подключите к вашему коду
# README_SAPI

def connect(client):
    client.connect((config["Server"], int(config["Port"])))

def send_responce(client, messages, stream):
    payload = json.dumps({"Messages": messages, "Stream": stream})
    client.send(payload.encode("utf-8"))

    buffer = ""
    while True:
        data = client.recv(4096)
        if not data:
            break

        buffer += data.decode("utf-8")

        while buffer:
            try:
                res, idx = json.JSONDecoder().raw_decode(buffer)
                buffer = buffer[idx:].lstrip()

                yield res

                if res.get("Type") == "SysM" and res.get("Message") == "Ready":
                    return
            except json.JSONDecodeError:
                break


config = {}

with open("client.conf", "r", encoding="utf-8") as file:
    config = json.load(file)

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
