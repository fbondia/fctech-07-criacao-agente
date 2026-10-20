import fcntl

import uvicorn

from aurora.config import Settings
from aurora.storage import Store


def serve():
    uvicorn.run("aurora.api:create_app", factory=True, host="127.0.0.1", port=8000, workers=1)


def reset():
    """Restaura dados e elimina sessões/pendências, somente com a API parada."""
    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Pare a API antes de restaurar os dados.") from None
        for name in ("condominio.sqlite3", "sessoes.sqlite3"):
            for suffix in ("", "-wal", "-shm"):
                (settings.data_dir / (name + suffix)).unlink(missing_ok=True)
        Store(settings.data_dir / "condominio.sqlite3")
    print(f"Dados iniciais restaurados em {settings.data_dir}; sessões e confirmações apagadas.")
