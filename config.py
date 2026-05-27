from pathlib import Path
import datetime

PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "raw"


def progress_filename(prefix: str) -> Path:
    today_str = datetime.date.today().strftime("%m.%d.%y")
    return RAW_DIR / f"{prefix}{today_str}.csv"
