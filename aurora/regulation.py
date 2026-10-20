"""Busca local por capítulo e artigo; o texto completo nunca sai do retriever."""

import math
import re
import unicodedata
from collections import Counter

from aurora.config import SEEDS

STOP = set(
    "a ao aos as o os de da das do dos e em no nos na nas por para que qual quais "
    "como ate um uma eu se com sobre meu minha pode posso sao ser tem horas horario "
    "funciona funcionamento regulamento regra regras interno condominio".split()
)


def tokens(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z]{3,}", text) if w not in STOP}


class Regulation:
    def __init__(self):
        text = (SEEDS / "regulamento.md").read_text()
        self.chapters = []
        for chapter in re.split(r"(?m)^## ", text)[1:]:
            title, body = chapter.split("\n", 1)
            articles = [a.strip() for a in re.split(r"(?=\*\*Art\.)", body) if a.strip()]
            self.chapters.append((title, articles))
        self.frequency = Counter(
            t for title, articles in self.chapters for t in tokens(title + " ".join(articles))
        )

    def search(self, query):
        words = tokens(query)
        if not words:
            return {"status": "especifique_assunto", "trechos": []}

        def score(text):
            return sum(
                math.log(1 + len(self.chapters) / self.frequency.get(w, 1))
                for w in words & tokens(text)
            )

        ranked = sorted(
            self.chapters,
            key=lambda c: 4 * score(c[0]) + max((score(a) for a in c[1]), default=0),
            reverse=True,
        )
        title, articles = ranked[0]
        matches = sorted(enumerate(articles), key=lambda a: score(a[1]), reverse=True)
        selected = [(i, a) for i, a in matches[:2] if score(a) > 0]
        return {
            "fonte": "dados/regulamento.md",
            "capitulo": title,
            "trechos": [a for _, a in sorted(selected)],
        }
