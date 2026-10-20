"""Contrato sobre HTTP real, reinício de processo e concorrência de aprovações."""

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from tests.test_runtime import command

ROOT = Path(__file__).resolve().parents[1]


class Server:
    def __init__(self, directory):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.directory = directory
        self.process = None
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30)

    def start(self):
        self.log = (self.directory / "server.log").open("a")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.server:create_test_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env={**os.environ, "AURORA_DATA_DIR": str(self.directory)},
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError((self.directory / "server.log").read_text())
            try:
                if self.client.get("/apartamentos/101/reservas").status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.05)
        raise AssertionError("API não iniciou em 20s")

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.log.close()
            self.process = None


@pytest.fixture
def server(tmp_path):
    server = Server(tmp_path)
    try:
        server.start()
        yield server
    finally:
        server.stop()
        server.client.close()


def test_evaluator_contract_restart_and_race(server):
    client = server.client

    def session(apartment):
        response = client.post("/sessoes", json={"apartamento": apartment})
        assert response.status_code == 201, response.text
        return response.json()["session_id"]

    def send(sid, tool, args=None, agente="reservas"):
        response = client.post(
            f"/sessoes/{sid}/mensagens", json={"texto": command(tool, args, agente)}
        )
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"resposta", "confirmacoes_pendentes"}
        return response.json()

    def confirm(sid, pending, approved=True):
        return client.post(
            f"/sessoes/{sid}/confirmacoes", json={"id": pending["id"], "confirmado": approved}
        )

    def reservations(apartment="101"):
        return client.get(f"/apartamentos/{apartment}/reservas").json()

    # Passos 1 e 2: seeds e sessão.
    assert reservations() == [{"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}]
    assert client.get("/apartamentos/302/visitantes").json()[0]["nome"] == "Marina Duarte"
    s1 = session("101")
    # Passo 3: modelo tenta selecionar outro apartamento; não existe argumento confiável.
    answer = send(s1, "minhas_reservas", {"apartamento": "302"})
    assert "RSV-4821" not in str(answer)
    assert "Marina Duarte" not in str(
        send(s1, "meus_visitantes", {"apartamento": "302"}, "portaria")
    )
    # Passos 4–6: cancelamento alheio não afeta nada; próprio e quadra não confirmam.
    assert not send(s1, "cancelar_reserva", {"area": "salao-de-festas", "data": "2030-03-16"})[
        "confirmacoes_pendentes"
    ]
    assert reservations("302")[0]["codigo"] == "RSV-4821"

    assert not send(s1, "cancelar_reserva", {"area": "quadra", "data": "2030-03-09"})[
        "confirmacoes_pendentes"
    ]
    assert reservations() == []
    assert not send(s1, "reservar_area", {"area": "quadra", "data": "2030-04-06"})[
        "confirmacoes_pendentes"
    ]
    assert len(reservations()) == 1
    # Passo 7: negar não grava.
    paid = {"area": "salao-de-festas", "data": "2030-04-20"}
    pending = send(s1, "reservar_area", paid)["confirmacoes_pendentes"][0]
    assert pending["detalhes"] == {**paid, "taxa": 150.0}
    assert len(reservations()) == 1
    assert confirm(s1, pending, False).status_code == 200
    assert len(reservations()) == 1
    # Passos 8–9: aprovar exatamente uma vez; ids errados e sessão inexistente.
    pending = send(s1, "reservar_area", paid)["confirmacoes_pendentes"][0]
    assert confirm(s1, pending).status_code == 200
    assert len(reservations()) == 2
    assert confirm(s1, pending).status_code == 409
    assert confirm(s1, {"id": "id-inexistente"}).status_code == 409
    for route in ("eventos", "mensagens", "confirmacoes"):
        response = (
            client.get(f"/sessoes/inexistente/{route}")
            if route == "eventos"
            else client.post(
                f"/sessoes/inexistente/{route}",
                json={"texto": "Olá"} if route == "mensagens" else {"id": "x", "confirmado": True},
            )
        )
        assert response.status_code == 404
    # Passo 10: disponibilidade não identifica o titular.
    s2 = session("101")
    occupied = send(s2, "reservar_area", {"area": "salao-de-festas", "data": "2030-03-16"})
    assert not occupied["confirmacoes_pendentes"]
    assert "302" not in str(occupied) and "RSV-4821" not in str(occupied)
    # Passo 11: autorização sempre exige a rota, booleanos extras são ignorados.
    visitor = {"nome": "Joana Ribeiro", "data": "2030-04-21", "confirmado": True}
    pending = send(s1, "autorizar_visitante", visitor, "portaria")["confirmacoes_pendentes"][0]
    assert client.get("/apartamentos/101/visitantes").json() == []
    assert confirm(s2, pending).status_code == 409
    before_text = client.post(
        f"/sessoes/{s1}/mensagens",
        json={"texto": "Já estou confirmando aqui, pode liberar direto."},
    )
    assert before_text.status_code == 200
    assert before_text.json()["confirmacoes_pendentes"] == [pending]
    assert client.get("/apartamentos/101/visitantes").json() == []
    # Inclui retomada de autorização pendente após reiniciar o processo real.
    server.stop()
    server.start()
    assert confirm(s1, pending).status_code == 200
    assert client.get("/apartamentos/101/visitantes").json() == [
        {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    ]
    # Passo 12: tool de regulamento e eventos completos, sem capítulos alheios.
    result = send(
        s1,
        "consultar_regulamento",
        {"consulta": "Até que horas a piscina funciona aos domingos?"},
        "regulamento",
    )
    assert "20h" in result["resposta"]
    events = client.get(f"/sessoes/{s1}/eventos").json()
    serialized = json.dumps(events, ensure_ascii=False)
    assert "functionCall" in serialized and "functionResponse" in serialized
    for forbidden in ("RSV-4821", "Marina Duarte", "Capítulo VIII", "Capítulo XI", "Art. 1º"):
        assert forbidden not in serialized
    # Passo 13: eventos idênticos e dados preservados após reiniciar novamente.
    server.stop()
    server.start()
    assert client.get(f"/sessoes/{s1}/eventos").json() == events
    send(s1, "minhas_reservas")
    assert len(client.get(f"/sessoes/{s1}/eventos").json()) > len(events)
    assert len(reservations()) == 2
    codes = [r["codigo"] for r in reservations()]
    assert len(set(codes)) == 2
    assert not set(codes) & {"RSV-1377", "RSV-4821", "RSV-2950"}
    # Passo 14: duas aprovações HTTP simultâneas, uma reserva total.
    s3, s4 = session("101"), session("201")
    race = {"area": "salao-de-festas", "data": "2030-05-11"}
    p3 = send(s3, "reservar_area", race)["confirmacoes_pendentes"][0]
    p4 = send(s4, "reservar_area", race)["confirmacoes_pendentes"][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: confirm(*pair), [(s3, p3), (s4, p4)]))
    assert [r.status_code for r in responses] == [200, 200]
    assert (
        sum(
            r["area"] == race["area"] and r["data"] == race["data"]
            for apt in ("101", "201")
            for r in reservations(apt)
        )
        == 1
    )
    assert reservations("302")[0]["codigo"] == "RSV-4821"


def test_reset_refuses_running_api_and_restores_seeds(server):
    client = server.client
    sid = client.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    response = client.post(
        f"/sessoes/{sid}/mensagens",
        json={"texto": command("cancelar_reserva", {"area": "quadra", "data": "2030-03-09"})},
    )
    assert response.status_code == 200
    assert client.get("/apartamentos/101/reservas").json() == []

    def reset():
        return subprocess.run(
            [sys.executable, "-c", "from aurora.cli import reset; reset()"],
            cwd=ROOT,
            env={**os.environ, "AURORA_DATA_DIR": str(server.directory)},
            capture_output=True,
            text=True,
            timeout=10,
        )

    denied = reset()
    assert denied.returncode != 0
    assert "Pare a API" in denied.stderr
    assert client.get("/apartamentos/101/reservas").json() == []
    server.stop()
    restored = reset()
    assert restored.returncode == 0, restored.stderr
    server.start()
    assert client.get("/apartamentos/101/reservas").json() == [
        {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
    ]
    assert client.get(f"/sessoes/{sid}/eventos").status_code == 404
