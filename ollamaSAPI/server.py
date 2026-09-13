import json
import socket
import ollama

# Этот файл запускается на сервере, для работы необходим server.conf
# Подробнее в README_SAPI.md

with open("server.conf", "r", encoding="utf-8") as file:
    config = json.load(file)

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind((config["ServerHost"], int(config["ServerPort"])))
server.listen()

clients = []

client_socket, _ = server.accept()
clients.append(client_socket)

while True:
    for client in clients:
        data = client.recv(config["BufferSize"])

        if not data:
            continue

        req = json.loads(data.decode("utf-8"))

        stream = ollama.chat(
            model=config["ModelData"]["ModelName"],
            messages=req["Messages"],
            stream=bool(req["Stream"]),
        )

        in_thinking = False
        content = ""
        thinking = ""

        for chunk in stream:
            chunk_thinking = getattr(chunk.message, "thinking", None)
            chunk_content = getattr(chunk.message, "content", None)

            if chunk_thinking:
                if not in_thinking:
                    in_thinking = True

                payload = json.dumps(
                    {
                        "Type": "Resp",
                        "Half": True,
                        "Thinking": True,
                        "Message": chunk_thinking,
                    }
                )
                client.send(payload.encode("utf-8"))
                thinking += chunk_thinking

            elif chunk_content:
                if in_thinking:
                    in_thinking = False

                payload = json.dumps(
                    {
                        "Type": "Resp",
                        "Half": True,
                        "Thinking": False,
                        "Message": chunk_content,
                    }
                )
                client.send(payload.encode("utf-8"))
                content += chunk_content

        chat_history = req.get("Messages", [])
        chat_history.append(
            {"role": "assistant", "thinking": thinking, "content": content}
        )

        payload_final = json.dumps(
            {
                "Type": "Resp",
                "Half": False,
                "Thinking": True,
                "Message": chat_history,
            }
        )
        client.send(payload_final.encode("utf-8"))

        payload_ready = json.dumps({"Type": "SysM", "Message": "Ready"})
        client.send(payload_ready.encode("utf-8"))
