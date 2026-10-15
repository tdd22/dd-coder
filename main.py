from agent.graph import run_chat


def main() -> None:
    print(run_chat("读取 hello.txt，只回复文件里的文字。"))


if __name__ == "__main__":
    main()