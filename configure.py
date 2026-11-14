"""Interactive local setup: token stays on the server, owner proves access in Telegram."""
import getpass
import os
from pathlib import Path
import secrets
import time

from bot import APIError, Telegram


def main():
    path = Path(".env")
    if path.exists():
        raise SystemExit(".env уже существует. Измените его вручную; настройка не перезаписывает секреты.")
    token = getpass.getpass("Вставьте токен BotFather (ввод скрыт): ").strip()
    if ":" not in token:
        raise SystemExit("Неверный формат токена.")
    api = Telegram(token)
    try:
        bot = api.call("getMe")
        if api.call("getWebhookInfo").get("url"):
            raise SystemExit("Бот подключён к другому сервису через webhook. Сначала отключите прежнее подключение.")
        nonce = secrets.token_hex(8)
        print(f"Откройте https://t.me/{bot['username']} в своём Telegram.")
        print(f"Отправьте боту личное сообщение:\n/setup {nonce}")
        print("Ожидаю до 5 минут. Другой экземпляр бота сейчас должен быть остановлен.")
        deadline = time.time() + 300
        offset = 0
        while time.time() < deadline:
            updates = api.call("getUpdates", offset=offset, timeout=10, allowed_updates=["message"])
            for update in updates:
                offset = update["update_id"]+1
                m = update.get("message", {})
                if m.get("chat", {}).get("type") != "private" or m.get("text", "").strip() != f"/setup {nonce}":
                    continue
                owner = m["from"]["id"]
                print(f"Найден владелец: {m['from'].get('first_name', '')}, Telegram ID {owner}.")
                if input("Это вы? Напишите YES для сохранения: ").strip() != "YES":
                    raise SystemExit("Настройка отменена.")
                value = (f"BOT_TOKEN={token}\nOWNER_ID={owner}\nADMIN_IDS=\n"
                         "GROUP_ID=\nDB_PATH=data/padel.sqlite3\n"
                         "LEVELS=D,D+,C-,C,C+,B-,B,B+,A\n")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(value)
                api.send(owner, "Настройки владельца сохранены. После запуска программы откройте /admin.\nТокен в Telegram не отправляйте.")
                print(".env создан с закрытым доступом. Теперь: python3 bot.py --check")
                return
        raise SystemExit("Время ожидания вышло. Запустите configure.py снова.")
    except APIError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
