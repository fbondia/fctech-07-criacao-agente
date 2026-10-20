"""Invariantes de negócio em transações SQLite, sem decisões do modelo."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from aurora.config import SEEDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (numero TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS areas (id TEXT PRIMARY KEY, nome TEXT NOT NULL, taxa REAL NOT NULL);
CREATE TABLE IF NOT EXISTS identidades (
    session_id TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero)
);
CREATE TRIGGER IF NOT EXISTS identidade_imutavel BEFORE UPDATE ON identidades
BEGIN SELECT RAISE(ABORT, 'Identidade imutável'); END;
CREATE TABLE IF NOT EXISTS reservas (
    codigo TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    area TEXT NOT NULL REFERENCES areas(id),
    data TEXT NOT NULL,
    ativa INTEGER NOT NULL DEFAULT 1 CHECK (ativa IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS reserva_exclusiva ON reservas(area, data) WHERE ativa=1;
CREATE TABLE IF NOT EXISTS visitantes (
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    nome TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(apartamento, nome, data)
);
CREATE TABLE IF NOT EXISTS confirmacoes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES identidades(session_id),
    original_id TEXT NOT NULL,
    agente TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    acao TEXT NOT NULL,
    detalhes TEXT NOT NULL,
    respondida INTEGER NOT NULL DEFAULT 0,
    resultado TEXT,
    UNIQUE(session_id, original_id)
);
"""


class ConfirmationConflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if not db.execute("SELECT 1 FROM apartamentos LIMIT 1").fetchone():
                self._seed(db)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _seed(db):
        def read(name):
            return json.loads((SEEDS / f"{name}.json").read_text())

        db.executemany(
            "INSERT INTO apartamentos VALUES (?)", [(a["numero"],) for a in read("apartamentos")]
        )
        db.executemany("INSERT INTO areas VALUES (:id,:nome,:taxa)", read("areas"))
        db.executemany(
            "INSERT INTO reservas(codigo,apartamento,area,data) "
            "VALUES (:codigo,:apartamento,:area,:data)",
            read("reservas"),
        )
        db.executemany(
            "INSERT INTO visitantes VALUES (:apartamento,:nome,:data)", read("visitantes")
        )

    def create_identity(self, session_id, apartamento):
        with self.connection(write=True) as db:
            db.execute("INSERT INTO identidades VALUES (?,?)", (session_id, apartamento))

    def identity(self, session_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT apartamento FROM identidades WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row:
            raise LookupError("Sessão inexistente")
        return row["apartamento"]

    def apartment_exists(self, apartamento):
        with self.connection() as db:
            return bool(
                db.execute("SELECT 1 FROM apartamentos WHERE numero=?", (apartamento,)).fetchone()
            )

    def areas(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM areas ORDER BY id")]

    def reservations(self, apartamento):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT codigo,area,data FROM reservas WHERE apartamento=? AND ativa=1 "
                    "ORDER BY data,codigo",
                    (apartamento,),
                )
            ]

    def visitors(self, apartamento):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT nome,data FROM visitantes WHERE apartamento=? ORDER BY data,nome",
                    (apartamento,),
                )
            ]

    def available(self, area, data):
        with self.connection() as db:
            return not db.execute(
                "SELECT 1 FROM reservas WHERE area=? AND data=? AND ativa=1", (area, data)
            ).fetchone()

    @staticmethod
    def _reserve(db, apartamento, details):
        # Códigos cancelados permanecem na tabela e continuam protegidos pela PK.
        code = f"RSV-{uuid4().hex}"
        try:
            db.execute(
                "INSERT INTO reservas(codigo,apartamento,area,data) VALUES (?,?,?,?)",
                (code, apartamento, details["area"], details["data"]),
            )
        except sqlite3.IntegrityError:
            # A constraint decide o vencedor no instante do INSERT; não revela o titular.
            return {"status": "ocupada", "mensagem": "Área ocupada nessa data."}
        return {"status": "reservada", "codigo": code, **details}

    def reserve_free(self, session_id, details):
        apartamento = self.identity(session_id)
        with self.connection(write=True) as db:
            area = db.execute("SELECT taxa FROM areas WHERE id=?", (details["area"],)).fetchone()
            if not area or area["taxa"] != 0:
                raise ValueError("Reserva paga exige confirmação sistêmica")
            return self._reserve(db, apartamento, details)

    def cancel(self, session_id, area, data):
        apartamento = self.identity(session_id)
        with self.connection(write=True) as db:
            count = db.execute(
                "UPDATE reservas SET ativa=0 WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                (apartamento, area, data),
            ).rowcount
        return {"status": "cancelada" if count else "nao_encontrada"}

    def register_confirmation(
        self, *, id, session_id, original_id, agente, invocation_id, acao, detalhes
    ):
        with self.connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO confirmacoes "
                "(id,session_id,original_id,agente,invocation_id,acao,detalhes) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    id,
                    session_id,
                    original_id,
                    agente,
                    invocation_id,
                    acao,
                    json.dumps(detalhes, ensure_ascii=False, sort_keys=True),
                ),
            )

    def confirmation(self, session_id, id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM confirmacoes WHERE session_id=? AND id=?", (session_id, id)
            ).fetchone()
        if row is None:
            raise ConfirmationConflict
        result = dict(row)
        result["detalhes"] = json.loads(result["detalhes"])
        if result["resultado"]:
            result["resultado"] = json.loads(result["resultado"])
        return result

    def pending(self, session_id):
        with self.connection() as db:
            return [
                {"id": r["id"], "acao": r["acao"], "detalhes": json.loads(r["detalhes"])}
                for r in db.execute(
                    "SELECT id,acao,detalhes FROM confirmacoes "
                    "WHERE session_id=? AND respondida=0 ORDER BY rowid",
                    (session_id,),
                )
            ]

    def resolve(self, session_id, id, original_id, acao, detalhes, approved):
        apartamento = self.identity(session_id)
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM confirmacoes WHERE session_id=? AND id=? "
                "AND original_id=? AND respondida=0",
                (session_id, id, original_id),
            ).fetchone()
            if not row or row["acao"] != acao or json.loads(row["detalhes"]) != detalhes:
                raise ConfirmationConflict
            if not approved:
                result = {"status": "negada", "mensagem": "Ação negada; nenhum efeito aplicado."}
            elif acao == "reservar_area":
                result = self._reserve(db, apartamento, detalhes)
            elif acao == "autorizar_visitante":
                db.execute(
                    "INSERT OR IGNORE INTO visitantes VALUES (?,?,?)",
                    (apartamento, detalhes["nome"], detalhes["data"]),
                )
                result = {"status": "autorizado", **detalhes}
            else:
                raise ValueError("Ação desconhecida")
            # Consumo da confirmação e efeito são uma única transação.
            db.execute(
                "UPDATE confirmacoes SET respondida=1,resultado=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), id),
            )
            return result
