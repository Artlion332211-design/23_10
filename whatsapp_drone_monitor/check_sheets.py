from __future__ import annotations

import sys

from src.config import Config
from src.sheets_client import open_registry

CONFIG_PATH = "config.yaml"


def main() -> int:
    config = Config.load(CONFIG_PATH)
    print(f"Підключення до таблиці {config.spreadsheet_id} через {config.credentials_path} ...")
    try:
        registry = open_registry(config.spreadsheet_id, config.credentials_path, config.model_sheets)
    except FileNotFoundError:
        print(f"ПОМИЛКА: не знайдено файл ключа сервісного акаунта: {config.credentials_path}")
        return 1
    except Exception as exc:
        print(f"ПОМИЛКА підключення: {exc}")
        print("Перевір: чи увімкнено Google Sheets API в проєкті, чи таблицю розшарено")
        print("на email сервісного акаунта (поле client_email у credentials.json) з правами Editor.")
        return 1

    print("Підключення успішне. Листи книги:")
    for title in registry.list_worksheet_titles():
        print(f"  - {title}")

    print("\nПробний пошук серійника з config.example.yaml (можна ігнорувати 'не знайдено'):")
    test_serial = "1581F7K3C264200DAFYJ"
    matches = registry.find_by_serial(test_serial)
    if matches:
        for m in matches:
            print(f"  Знайдено на листі '{m.worksheet.title}', рядок {m.row}, статус зараз: {m.old_status}")
    else:
        print(f"  Серійник {test_serial} не знайдено (ОК, якщо це не той борт).")

    print("\nЛист «На позиції»:")
    print(registry.read_position_summary(config.position_summary_sheet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
