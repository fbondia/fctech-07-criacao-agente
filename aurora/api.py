import fcntl
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from aurora.config import Settings
from aurora.runtime import Runtime
from aurora.storage import ConfirmationConflict

logger = logging.getLogger(__name__)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(Body):
    apartamento: str = Field(min_length=1, max_length=20)


class Message(Body):
    texto: str = Field(min_length=1, max_length=10000)


class Confirmation(Body):
    id: str = Field(min_length=1, max_length=200)
    confirmado: StrictBool


def create_app(settings=None, *, model=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with (settings.data_dir / "server.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Use um único processo da API por AURORA_DATA_DIR") from exc
            app.state.runtime = Runtime(settings, model=model)
            try:
                yield
            finally:
                await app.state.runtime.runner.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    app = FastAPI(title="Residencial Aurora", lifespan=lifespan)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConfirmationConflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": "Confirmação não está pendente"})

    async def conversation(awaitable):
        try:
            return await awaitable
        except (LookupError, ConfirmationConflict):
            raise
        except Exception as exc:
            # Não enviar payloads do provedor, históricos ou credenciais ao cliente/log.
            logger.warning("Execução ADK indisponível (%s)", type(exc).__name__)
            raise HTTPException(
                503,
                "Não foi possível concluir a conversa. "
                "Verifique a chave, o modelo e a cota Gemini.",
            ) from None

    @app.post("/sessoes", status_code=201)
    async def create(body: NewSession):
        return {"session_id": await app.state.runtime.create_session(body.apartamento)}

    @app.post("/sessoes/{session_id}/mensagens")
    async def message(session_id: str, body: Message):
        return await conversation(app.state.runtime.message(session_id, body.texto))

    @app.post("/sessoes/{session_id}/confirmacoes")
    async def confirmation(session_id: str, body: Confirmation):
        return await conversation(app.state.runtime.confirm(session_id, body.id, body.confirmado))

    @app.get("/sessoes/{session_id}/eventos")
    async def events(session_id: str):
        return await app.state.runtime.events(session_id)

    @app.get("/apartamentos/{apartamento}/reservas")
    def reservations(apartamento: str):
        return app.state.runtime.store.reservations(apartamento)

    @app.get("/apartamentos/{apartamento}/visitantes")
    def visitors(apartamento: str):
        return app.state.runtime.store.visitors(apartamento)

    return app
