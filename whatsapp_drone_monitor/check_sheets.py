from __future__ import annotations

import sys

from src.config import Config
from src.reporter import format_position_stats
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

    print("Підключення успішне. Листи книги (це може зайняти кілька секунд):")
    titles = registry.list_worksheet_titles()
    for title in titles:
        print(f"  - {title}")

    print(f"\nПробний пошук серійника з {config.spreadsheet_id} (можна ігнорувати 'не знайдено'):")
    test_serial = "1581F7K3C264200DAFYJ"
    matches = registry.find_by_serial(test_serial)
    if matches:
        for m in matches:
            print(f"  Знайдено на листі '{m.worksheet.title}', рядок {m.row}, статус зараз: {m.old_status}")
    else:
        print(f"  Серійник {test_serial} не знайдено (ОК, якщо це не той борт).")

    print(f"\nЛист «{config.position_summary_sheet}» (денні/нічні/ремонт по позиціях):")
    stats = registry.read_position_stats(config.daily_report_groups, config.position_summary_sheet)
    print(format_position_stats(stats))
    for s in stats:
        if not s.found:
            print(f"  УВАГА: групу «{s.group}» не знайдено на цьому листі — звір назву в config.yaml.")

    if config.loss_log_sheet not in titles:
        print(f"\nУВАГА: листа «{config.loss_log_sheet}» (журнал втрат) немає серед листів книги — "
              f"перевір назву в config.yaml (loss_log_sheet).")
    else:
        header = registry.read_header(config.loss_log_sheet)
        print(f"\nЛист «{config.loss_log_sheet}» знайдено. Заголовок першого рядка: {header}")
        if "Серійний номер" not in header:
            print("  УВАГА: у заголовку немає 'Серійний номер' — бот писатиме новий запис")
            print("  за позицією колонок з коду (FALLBACK_LOSS_COLUMNS), а не за назвою.")
            print("  Перевір руками перший тестовий запис після реальної втрати.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
