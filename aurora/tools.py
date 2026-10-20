from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date

from google.adk.tools import ToolContext

from aurora.regulation import Regulation, tokens
from aurora.storage import Store


@dataclass(frozen=True)
class Approval:
    session_id: str
    confirmation_id: str
    original_id: str
    confirmed: bool


# Somente a rota HTTP de confirmações define este contexto, nunca o modelo.
approval_context: ContextVar[Approval | None] = ContextVar("approval", default=None)


def valid_date(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use uma data AAAA-MM-DD")
    return value


def build_tools(store: Store):
    regulation = Regulation()

    def area_details(area, data):
        valid_date(data)
        for item in store.areas():
            if area == item["id"] or tokens(area) == tokens(item["nome"]):
                return {"area": item["id"], "data": data, "taxa": item["taxa"]}
        raise ValueError("Área desconhecida; consulte listar_areas")

    def gated(action, details, ctx):
        store.identity(ctx.session.id)
        grant = approval_context.get()
        if (
            grant
            and grant.session_id == ctx.session.id
            and grant.original_id == ctx.function_call_id
            and ctx.tool_confirmation is not None
        ):
            return store.resolve(
                ctx.session.id,
                grant.confirmation_id,
                grant.original_id,
                action,
                details,
                grant.confirmed,
            )
        # Nem um booleano nos argumentos nem texto de conversa concede permissão.
        ctx.request_confirmation(
            hint="Responda exclusivamente pela rota de confirmações da sessão.",
            payload={"acao": action, "detalhes": details},
        )
        return {"status": "pendente", "acao": action, "detalhes": details}

    def listar_areas():
        """Lista identificadores, nomes e taxas oficiais das áreas reserváveis."""
        return {"areas": store.areas()}

    def minhas_reservas(tool_context: ToolContext):
        """Consulta as reservas atuais exclusivamente do apartamento autenticado."""
        return {"reservas": store.reservations(store.identity(tool_context.session.id))}

    def consultar_disponibilidade(area: str, data: str, tool_context: ToolContext):
        """Informa somente se uma área está livre/ocupada na data AAAA-MM-DD."""
        store.identity(tool_context.session.id)
        try:
            details = area_details(area, data)
        except ValueError as exc:
            return {"erro": str(exc)}
        return {
            "area": details["area"],
            "data": data,
            "livre": store.available(details["area"], data),
        }

    def reservar_area(area: str, data: str, tool_context: ToolContext):
        """Reserva área para o morador da sessão. Taxa positiva exige confirmação na API.

        Args:
            area: Identificador oficial da área, obtido em listar_areas.
            data: Data da reserva em AAAA-MM-DD.
        """
        try:
            details = area_details(area, data)
        except ValueError as exc:
            return {"erro": str(exc)}
        store.identity(tool_context.session.id)
        # Na retomada, revalidar a disponibilidade dentro do INSERT transacional.
        if tool_context.tool_confirmation is None and not store.available(details["area"], data):
            return {"status": "ocupada", "mensagem": "Área ocupada nessa data."}
        if details["taxa"] > 0:
            return gated("reservar_area", details, tool_context)
        return store.reserve_free(tool_context.session.id, details)

    def cancelar_reserva(area: str, data: str, tool_context: ToolContext):
        """Cancela sem confirmação somente reserva própria pela área e data AAAA-MM-DD."""
        try:
            details = area_details(area, data)
        except ValueError as exc:
            return {"erro": str(exc)}
        return store.cancel(tool_context.session.id, details["area"], data)

    def meus_visitantes(tool_context: ToolContext):
        """Lista visitantes autorizados exclusivamente pelo apartamento autenticado."""
        return {"visitantes": store.visitors(store.identity(tool_context.session.id))}

    def autorizar_visitante(nome: str, data: str, tool_context: ToolContext):
        """Solicita autorização de entrada; sempre exige confirmação pela API.

        Args:
            nome: Nome completo do visitante.
            data: Data da visita em AAAA-MM-DD.
        """
        try:
            valid_date(data)
        except ValueError:
            return {"erro": "Informe a data da visita em AAAA-MM-DD"}
        nome = " ".join(nome.split())
        if not nome or len(nome) > 200:
            return {"erro": "Informe um nome entre 1 e 200 caracteres"}
        return gated("autorizar_visitante", {"nome": nome, "data": data}, tool_context)

    def consultar_regulamento(consulta: str):
        """Recupera até dois artigos de um único capítulo pertinente à dúvida específica."""
        return regulation.search(consulta)

    return {
        "reservas": [
            listar_areas,
            minhas_reservas,
            consultar_disponibilidade,
            reservar_area,
            cancelar_reserva,
        ],
        "portaria": [meus_visitantes, autorizar_visitante],
        "regulamento": [consultar_regulamento],
    }
