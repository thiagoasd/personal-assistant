"""Gastos: gravação dos lançamentos, categorias por regra e resumo do mês."""
import sqlite3
import unicodedata
from dataclasses import dataclass

from .db import utcnow


@dataclass(frozen=True)
class Incoming:
    external_id: str
    date: str          # AAAA-MM-DD
    amount_cents: int  # negativo = saída
    description: str
    pending: bool = False


def normalize(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


def rule_category(conn: sqlite3.Connection, description: str) -> int | None:
    """Regra com o texto mais longo vence (mais específica)."""
    text = normalize(description)
    best = None
    for rule in conn.execute("SELECT pattern, category_id FROM rules"):
        if rule["pattern"] and rule["pattern"] in text:
            if best is None or len(rule["pattern"]) > len(best[0]):
                best = (rule["pattern"], rule["category_id"])
    return best[1] if best else None


def ingest(conn: sqlite3.Connection, account_id: int, incoming: list[Incoming]) -> dict:
    """Grava novos e atualiza os existentes. Categoria escolhida à mão nunca é trocada."""
    inserted = updated = 0
    for tx in incoming:
        row = conn.execute(
            "SELECT id, category_manual, category_id FROM transactions WHERE account_id = ? AND external_id = ?",
            (account_id, tx.external_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO transactions (account_id, external_id, date, amount_cents, description, "
                "category_id, pending, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (account_id, tx.external_id, tx.date, tx.amount_cents, tx.description[:300],
                 rule_category(conn, tx.description), int(tx.pending), utcnow()),
            )
            inserted += 1
        else:
            category = row["category_id"]
            if not row["category_manual"] and category is None:
                category = rule_category(conn, tx.description)
            conn.execute(
                "UPDATE transactions SET date = ?, amount_cents = ?, description = ?, pending = ?, "
                "category_id = ? WHERE id = ?",
                (tx.date, tx.amount_cents, tx.description[:300], int(tx.pending), category, row["id"]),
            )
            updated += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated}


def set_category(conn: sqlite3.Connection, tx_id: int, category_id: int | None, make_rule: str = "") -> int:
    """Categoriza um lançamento. Com `make_rule`, cria a regra e aplica nos ainda sem categoria."""
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_manual = 1 WHERE id = ?", (category_id, tx_id)
    )
    applied = 0
    pattern = normalize(make_rule)
    if pattern and category_id is not None:
        conn.execute(
            "INSERT INTO rules (pattern, category_id, created_at) VALUES (?,?,?) "
            "ON CONFLICT (pattern) DO UPDATE SET category_id = excluded.category_id",
            (pattern, category_id, utcnow()),
        )
        applied = apply_rules(conn)
    conn.commit()
    return applied


def apply_rules(conn: sqlite3.Connection) -> int:
    """Aplica as regras nos lançamentos sem categoria escolhida à mão e ainda sem categoria."""
    applied = 0
    for tx in conn.execute(
        "SELECT id, description FROM transactions WHERE category_manual = 0 AND category_id IS NULL"
    ).fetchall():
        category = rule_category(conn, tx["description"])
        if category is not None:
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (category, tx["id"]))
            applied += 1
    conn.commit()
    return applied


def month_summary(conn: sqlite3.Connection, month: str) -> dict:
    """Gastos e entradas do mês (AAAA-MM). Categorias marcadas como "não entra" ficam fora.

    `spent` é positivo (quanto saiu). Estorno numa categoria de gasto reduz o gasto dela.
    """
    rows = conn.execute(
        "SELECT c.id, COALESCE(c.name, 'Sem categoria') AS name, c.ignore_in_totals, "
        "c.monthly_limit_cents, SUM(t.amount_cents) AS total, "
        "SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents ELSE 0 END) AS outflow, "
        "SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) AS inflow, COUNT(*) AS count "
        "FROM transactions t LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE substr(t.date, 1, 7) = ? GROUP BY c.id",
        (month,),
    ).fetchall()
    categories, income, spent, uncategorized = [], 0, 0, 0
    for row in rows:
        if row["ignore_in_totals"]:
            continue
        if row["id"] is None:
            # Sem categoria: entradas e saídas separadas, nada se compensa.
            uncategorized = row["count"]
            income += row["inflow"]
            value = -row["outflow"]
        elif row["name"] == "Receitas":
            income += row["total"]
            continue
        elif row["total"] >= 0:
            income += row["total"]
            continue
        else:
            value = -row["total"]  # estorno da mesma categoria reduz o gasto
        if value <= 0:
            continue
        spent += value
        categories.append({
            "id": row["id"], "name": row["name"], "spent": value, "count": row["count"],
            "limit": row["monthly_limit_cents"],
            "over": bool(row["monthly_limit_cents"]) and value > row["monthly_limit_cents"],
        })
    categories.sort(key=lambda c: -c["spent"])
    return {"month": month, "spent": spent, "income": income, "categories": categories,
            "uncategorized": uncategorized}
