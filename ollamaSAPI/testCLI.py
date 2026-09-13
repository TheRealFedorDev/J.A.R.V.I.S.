import sys
from client import client, send_responce, connect


def main():
    messages = []

    connect(client)

    print("=== Ollama CLI (exit/quit для выхода) ===")

    while True:
        try:
            user_input = input("\nYou > ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            messages.append({"role": "user", "content": user_input})
            print("AI > ", end="", flush=True)

            in_thinking = False

            for chunk in send_responce(client, messages, True):
                if chunk.get("Type") == "Resp":
                    msg = chunk.get("Message", "")
                    is_thinking = chunk.get("Thinking", False)

                    if is_thinking:
                        if not in_thinking:
                            print("\n[Thinking...]\n", end="", flush=True)
                            in_thinking = True
                        print(msg, end="", flush=True)
                    else:
                        if in_thinking:
                            print("\n[Answer]\n", end="", flush=True)
                            in_thinking = False
                        print(msg, end="", flush=True)

            print()

        except (KeyboardInterrupt, EOFError):
            print("\nВыход...")
            break

    client.close()


if __name__ == "__main__":
    main()
