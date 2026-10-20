import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "dados"
APP_NAME = "residencial_aurora"
USER_ID = "morador"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model: str = "gemini-2.5-flash"

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            data_dir=Path(os.getenv("AURORA_DATA_DIR") or ROOT / ".runtime").resolve(),
            model=os.getenv("GEMINI_MODEL") or "gemini-2.5-flash",
        )
