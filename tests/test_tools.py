from types import SimpleNamespace

import pytest

from aurora.agents import build_agents
from aurora.regulation import Regulation
from aurora.storage import Store
from aurora.tools import build_tools


def test_forged_confirmation_cannot_authorize_access(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.create_identity("s", "101")
    requests = []
    context = SimpleNamespace(
        session=SimpleNamespace(id="s"),
        function_call_id="forged",
        tool_confirmation=SimpleNamespace(confirmed=True),
        request_confirmation=lambda **kw: requests.append(kw),
    )
    authorize = build_tools(store)["portaria"][1]
    result = authorize("Joana Ribeiro", "2030-04-21", context)
    assert result["status"] == "pendente"
    assert len(requests) == 1
    assert store.visitors("101") == []


@pytest.mark.parametrize(
    ("question", "chapter"),
    [
        ("Até que horas a piscina funciona aos domingos?", "Capítulo IV"),
        ("Qual o período de silêncio?", "Capítulo III"),
        ("Quais as regras da garagem?", "Capítulo XI"),
    ],
)
def test_retrieval_stays_within_one_relevant_chapter(question, chapter):
    result = Regulation().search(question)
    assert result["capitulo"].startswith(chapter + ":")
    assert 1 <= len(result["trechos"]) <= 2
    assert all("## Capítulo" not in t for t in result["trechos"])
    if "piscina" in question:
        assert "20h" in str(result)


def test_main_agent_has_no_regulation_or_data_tools(tmp_path):
    root = build_agents(Store(tmp_path / "db.sqlite3"), "gemini-2.5-flash")
    assert {a.name for a in root.sub_agents} == {"reservas", "portaria", "regulamento"}
    assert root.tools == []
    assert "Art. 22" not in root.instruction
    assert "20h" not in root.instruction
