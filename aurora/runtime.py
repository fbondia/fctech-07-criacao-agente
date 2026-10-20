import asyncio
from uuid import uuid4

from google.adk.apps import App
from google.adk.apps.app import ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from aurora.agents import build_agents
from aurora.config import APP_NAME, USER_ID, Settings
from aurora.storage import ConfirmationConflict, Store
from aurora.tools import Approval, approval_context


class Runtime:
    def __init__(self, settings: Settings, model=None):
        self.store = Store(settings.data_dir / "condominio.sqlite3")
        self.sessions = SqliteSessionService(str(settings.data_dir / "sessoes.sqlite3"))
        self.root = build_agents(self.store, model if model is not None else settings.model)
        self.app = App(
            name=APP_NAME,
            root_agent=self.root,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        self.runner = Runner(app=self.app, session_service=self.sessions)
        self._locks = {}

    def lock(self, session_id):
        # O servidor tem um único worker; sessões diferentes continuam concorrentes.
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def create_session(self, apartamento):
        if not self.store.apartment_exists(apartamento):
            raise LookupError("Apartamento inexistente")
        session_id = str(uuid4())
        self.store.create_identity(session_id, apartamento)
        await self.sessions.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state={"apartamento": apartamento},
        )
        return session_id

    async def session(self, session_id):
        self.store.identity(session_id)
        session = await self.sessions.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
        if session is None:
            raise LookupError("Sessão inexistente")
        # Reconstituir o índice de pendências inclusive se houve interrupção entre
        # a persistência do evento ADK e a indexação da confirmação.
        for event in session.events:
            self._index_confirmations(session_id, event)
        return session

    def _index_confirmations(self, session_id, event):
        for call in event.get_function_calls():
            if call.name != "adk_request_confirmation":
                continue
            args = call.args or {}
            original = args["originalFunctionCall"]
            payload = args["toolConfirmation"]["payload"]
            if original["name"] not in {"reservar_area", "autorizar_visitante"}:
                continue
            self.store.register_confirmation(
                id=call.id,
                session_id=session_id,
                original_id=original["id"],
                agente=event.author,
                invocation_id=event.invocation_id,
                acao=payload["acao"],
                detalhes=payload["detalhes"],
            )

    def response(self, session_id, text=""):
        return {"resposta": text, "confirmacoes_pendentes": self.store.pending(session_id)}

    async def _run(self, session_id, content, invocation_id=None):
        texts = []
        async for event in self.runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=content,
            invocation_id=invocation_id,
        ):
            self._index_confirmations(session_id, event)
            if event.content and not event.partial:
                texts.extend(p.text for p in event.content.parts or [] if p.text and not p.thought)
        return "\n".join(texts)

    async def message(self, session_id, text):
        async with self.lock(session_id):
            await self.session(session_id)
            if self.store.pending(session_id):
                return self.response(session_id, "Responda à confirmação pendente pelo aplicativo.")
            answer = await self._run(
                session_id, types.Content(role="user", parts=[types.Part(text=text)])
            )
            return self.response(session_id, answer)

    async def confirm(self, session_id, id, confirmed):
        async with self.lock(session_id):
            await self.session(session_id)
            pending = self.store.confirmation(session_id, id)
            if pending["respondida"]:
                raise ConfirmationConflict
            token = approval_context.set(
                Approval(session_id, id, pending["original_id"], confirmed)
            )
            try:
                content = types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=id,
                                name="adk_request_confirmation",
                                response={
                                    "confirmed": confirmed,
                                    "payload": {
                                        "acao": pending["acao"],
                                        "detalhes": pending["detalhes"],
                                    },
                                },
                            )
                        )
                    ],
                )
                try:
                    # Mesma invocation + App resumível: Runner encontra o especialista
                    # que emitiu o pedido, mesmo após reconstruir o processo.
                    answer = await self._run(session_id, content, pending["invocation_id"])
                except Exception:
                    result = self.store.confirmation(session_id, id)
                    if not result["respondida"]:
                        raise
                    # O efeito já foi confirmado atomicamente. Uma falha posterior do
                    # Gemini não deve fazer a API alegar que a operação não aconteceu.
                    answer = str(result["resultado"])
                if not self.store.confirmation(session_id, id)["respondida"]:
                    raise RuntimeError("ADK não retomou a tool; confirmação permanece pendente")
                return self.response(session_id, answer)
            finally:
                approval_context.reset(token)

    async def events(self, session_id):
        async with self.lock(session_id):
            session = await self.session(session_id)
            return [
                event.model_dump(mode="json", by_alias=True, exclude_none=False)
                for event in session.events
            ]
