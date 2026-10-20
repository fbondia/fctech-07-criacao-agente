import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from aurora.config import SEEDS
from aurora.storage import ConfirmationConflict, Store


def test_identity_is_immutable_and_cancel_is_scoped(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.create_identity("s1", "101")
    with pytest.raises(sqlite3.IntegrityError, match="Identidade imutável"):
        with store.connection(write=True) as db:
            db.execute("UPDATE identidades SET apartamento='302' WHERE session_id='s1'")
    assert store.identity("s1") == "101"
    assert store.cancel("s1", "salao-de-festas", "2030-03-16")["status"] == "nao_encontrada"
    assert store.reservations("302")[0]["codigo"] == "RSV-4821"
    with pytest.raises(ValueError, match="confirmação"):
        store.reserve_free("s1", {"area": "salao-de-festas", "data": "2030-04-20"})


def test_canceled_codes_remain_reserved_and_state_survives_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    store.create_identity("s1", "101")
    store.cancel("s1", "quadra", "2030-03-09")
    result = store.reserve_free("s1", {"area": "quadra", "data": "2030-03-09"})
    assert result["codigo"] != "RSV-1377"
    store = Store(path)
    assert store.reservations("101")[0]["codigo"] == result["codigo"]
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection(write=True) as db:
            db.execute("INSERT INTO reservas VALUES ('RSV-1377','101','quadra','2031-01-01',1)")


def test_atomic_confirmation_consumption_and_insert_race(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    details = {"area": "salao-de-festas", "data": "2030-05-11", "taxa": 150.0}
    for session, apartment in (("s1", "101"), ("s2", "201")):
        store.create_identity(session, apartment)
        store.register_confirmation(
            id=session,
            session_id=session,
            original_id=session,
            agente="reservas",
            invocation_id="inv",
            acao="reservar_area",
            detalhes=details,
        )
    barrier = Barrier(2)

    def approve(session):
        barrier.wait()
        return store.resolve(session, session, session, "reservar_area", details, True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, ["s1", "s2"]))
    assert sorted(r["status"] for r in results) == ["ocupada", "reservada"]
    assert store.pending("s1") == store.pending("s2") == []
    with pytest.raises(ConfirmationConflict):
        approve_again = store.resolve("s1", "s1", "s1", "reservar_area", details, True)
        assert approve_again is None


def test_confirmation_cannot_change_payload_or_session(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.create_identity("s1", "101")
    store.create_identity("s2", "201")
    details = {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    store.register_confirmation(
        id="c",
        session_id="s1",
        original_id="tool",
        agente="portaria",
        invocation_id="inv",
        acao="autorizar_visitante",
        detalhes=details,
    )
    for session, payload in (("s2", details), ("s1", {**details, "nome": "Outra Pessoa"})):
        with pytest.raises(ConfirmationConflict):
            store.resolve(session, "c", "tool", "autorizar_visitante", payload, True)
    assert store.visitors("101") == []
    assert len(store.pending("s1")) == 1


def test_original_data_checksums():
    expected = {
        "apartamentos.json": "d4b297899313f37e5bbb3ab9d1f7f070bb5985feca06058c5bce32c8c970b44b",
        "areas.json": "e56489365f156bd1be75961a3ccc965bf72cc2cef998bc5863c78ddded44c619",
        "regulamento.md": "86893f3b1471c06ec48c4004d2a9739134b22fac4ec5802b5d10a554cb5f4494",
        "reservas.json": "a1b849ac213ee8c208907e525e42d45f6ffe544710b7eed53a60f195e1a87197",
        "visitantes.json": "15a98d85b92479f5fa4f242d475c7ed4a2f7f4cd5826d2d0b867fa68c1fb88d5",
    }
    for file, checksum in expected.items():
        assert hashlib.sha256((SEEDS / file).read_bytes()).hexdigest() == checksum
    assert len(json.loads((SEEDS / "reservas.json").read_text())) == 3
