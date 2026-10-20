import json

from aurora.config import Settings
from aurora.runtime import Runtime
from tests.fake_model import ScriptedModel


def command(tool, args=None, agente="reservas"):
    return json.dumps({"agente": agente, "tool": tool, "args": args or {}})


async def test_adk_confirmation_after_restart(tmp_path):
    settings = Settings(tmp_path)
    runtime = Runtime(settings, model=ScriptedModel())
    session = await runtime.create_session("101")
    answer = await runtime.message(
        session, command("reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})
    )
    assert len(answer["confirmacoes_pendentes"]) == 1
    id = answer["confirmacoes_pendentes"][0]["id"]
    assert len(runtime.store.reservations("101")) == 1
    before = await runtime.events(session)
    await runtime.runner.close()
    runtime = Runtime(settings, model=ScriptedModel())
    assert await runtime.events(session) == before
    result = await runtime.confirm(session, id, True)
    assert not result["confirmacoes_pendentes"]
    assert len(runtime.store.reservations("101")) == 2
    await runtime.runner.close()
