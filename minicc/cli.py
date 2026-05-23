from minicc.agent import agent_loop


def main():
    history = []
    while True:
        try:
            query = input("Query: ")
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            continue
        if query.strip().lower() in (
            "q",
            "exit",
            "quit",
        ):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]

        if isinstance(response_content, list):
            # if the response_content is a list, print the text of each block
            for block in response_content:
                if hasattr(
                    block, "text"
                ):
                    print(block.text)


if __name__ == "__main__":
    main()
