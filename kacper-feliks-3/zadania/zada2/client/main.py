import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "generated"))
sys.path.insert(0, HERE)

from src.session import Session
from src.tui import ClientApp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="default")
    args = parser.parse_args()

    session = Session(args.name)
    print(f"[client] name={args.name} session-id={session.id}")
    app = ClientApp(session)
    app.run()


if __name__ == "__main__":
    main()
