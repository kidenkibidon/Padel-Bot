"""Create a consistent SQLite backup without stopping the bot."""
import argparse
import os
from pathlib import Path

from core import Store


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("destination")
    a = p.parse_args()
    source, destination = Path(a.source), Path(a.destination)
    if not source.is_file():
        raise SystemExit("Исходная база не найдена.")
    if destination.exists() or destination.resolve() == source.resolve():
        raise SystemExit("Путь назначения уже существует. Укажите новый файл.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    Store(source).backup(destination)
    os.chmod(destination, 0o600)
    print(destination)


if __name__ == "__main__":
    main()
