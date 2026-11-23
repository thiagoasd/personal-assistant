"""Leitura das contas pelo Pluggy (Open Finance). Só leitura: nunca inicia pagamentos.

Cliente e conversão de sinal adaptados de `analisador-gastos/gastos/pluggy.py`.
"""
import logging
import sqlite3
from datetime import date as Date, datetime, timedelta
from decimal import Decimal

import requests

from .config import TZ, Config
from .finance import Incoming, ingest
from .db import utcnow

log = logging.getLogger(__name__)

BASE_URL = "https://api.pluggy.ai"
TIMEOUT = 30
FIRST_SYNC_DAYS = 365
OVERLAP_DAYS = 10


class PluggyError(RuntimeError):
    pass


class PluggyClient:
    def __init__(self, client_id: str, client_secret: str, http=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or requests.Session()
        self.api_key: str | None = None

    def _authenticate(self) -> None:
        response = self.http.post(
            f"{BASE_URL}/auth",
            json={"clientId": self.client_id, "clientSecret": self.client_secret},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise PluggyError(f"Credenciais recusadas pelo Pluggy (HTTP {response.status_code}).")
        self.api_key = response.json()["apiKey"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.api_key:
            self._authenticate()
        response = self.http.get(
            f"{BASE_URL}{path}", params=params, headers={"X-API-KEY": self.api_key}, timeout=TIMEOUT
        )
        if response.status_code == 401:  # a chave dura 2 horas
            self._authenticate()
            response = self.http.get(
                f"{BASE_URL}{path}", params=params, headers={"X-API-KEY": self.api_key}, timeout=TIMEOUT
            )
        if response.status_code != 200:
            raise PluggyError(f"Pluggy respondeu HTTP {response.status_code} em {path.split('?')[0]}.")
        return response.json()

    def item(self, item_id: str) -> dict:
        return self._get(f"/items/{item_id}")

    def accounts(self, item_id: str) -> list[dict]:
        return self._get("/accounts", {"itemId": item_id}).get("results", [])

    def transactions(self, account_id: str, date_from: str) -> list[dict]:
        """Percorre o cursor do GET /v2/transactions até `next` vir nulo."""
        page = self._get("/v2/transactions", {"accountId": account_id, "dateFrom": date_from})
        results = list(page.get("results", []))
        seen = set()
        while page.get("next"):
            cursor = page["next"]
            if cursor in seen or not cursor.startswith("?"):
                raise PluggyError("Cursor de paginação inesperado do Pluggy.")
            seen.add(cursor)
            page = self._get(f"/v2/transactions{cursor}")
            results.extend(page.get("results", []))
        return results


def to_cents(raw) -> int:
    return int((Decimal(str(raw)) * 100).quantize(Decimal("1")))


def local_date(stamp: str) -> str:
    """Meia-noite UTC significa "só a data"; com hora real, converte para São Paulo."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if moment.tzinfo is None or (moment.hour, moment.minute, moment.second) == (0, 0, 0):
        return moment.date().isoformat()
    return moment.astimezone(TZ).date().isoformat()


def to_incoming(tx: dict, account_type: str, today: str) -> Incoming | None:
    """Sinal interno: negativo = saída.

    Cartão: `amount` positivo é compra, negativo é pagamento/estorno. Conta: direção vem de `type`.
    Pendente: no cartão são compras da fatura aberta (entram); na conta podem nem acontecer.
    """
    pending = tx.get("status") == "PENDING"
    day = local_date(tx["date"])
    if pending and (account_type != "CREDIT" or day > today):
        return None
    raw = tx.get("amountInAccountCurrency")
    if raw is None:
        raw = tx.get("amount")
    cents = to_cents(raw)
    if account_type == "CREDIT":
        signed = -cents
    else:
        signed = -abs(cents) if tx.get("type") == "DEBIT" else abs(cents)
    if signed == 0:
        return None
    description = (tx.get("description") or tx.get("descriptionRaw") or "(sem descrição)").replace("|", " - ")
    merchant = (tx.get("merchant") or {}).get("businessName")
    if merchant and merchant.lower() not in description.lower():
        description = f"{description} - {merchant}"
    return Incoming(tx["id"], day, signed, description.strip(), pending)


def _account(conn: sqlite3.Connection, item: sqlite3.Row, remote: dict) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE pluggy_account_id = ?", (remote["id"],)).fetchone()
    if row:
        return row["id"]
    kind = "card" if remote.get("type") == "CREDIT" else "checking"
    name = f"{item['label']} · {remote.get('name') or remote['id'][:8]}"
    return conn.execute(
        "INSERT INTO accounts (name, kind, pluggy_account_id, pluggy_item_id, created_at) VALUES (?,?,?,?,?)",
        (name, kind, remote["id"], item["item_id"], utcnow()),
    ).lastrowid


def sync_item(conn: sqlite3.Connection, client: PluggyClient, item: sqlite3.Row) -> int:
    today = datetime.now(TZ).date()
    remote_item = client.item(item["item_id"])
    inserted = 0
    for remote in client.accounts(item["item_id"]):
        account_id = _account(conn, item, remote)
        conn.commit()  # nunca deixar transação aberta durante a próxima chamada
        has_data = conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? LIMIT 1", (account_id,)
        ).fetchone()
        if has_data and item["last_sync_at"]:
            start = Date.fromisoformat(item["last_sync_at"][:10]) - timedelta(days=OVERLAP_DAYS)
        else:
            start = today - timedelta(days=FIRST_SYNC_DAYS)
        raw = client.transactions(remote["id"], start.isoformat())
        incoming = [t for t in (to_incoming(tx, remote.get("type"), today.isoformat()) for tx in raw) if t]
        inserted += ingest(conn, account_id, incoming)["inserted"]
        # Compra pendente que o banco deixou de informar foi cancelada.
        returned = {tx["id"] for tx in raw}
        for row in conn.execute(
            "SELECT id, external_id FROM transactions WHERE account_id = ? AND pending = 1 AND date >= ?",
            (account_id, start.isoformat()),
        ).fetchall():
            if row["external_id"] not in returned:
                conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
        balance = remote.get("balance")
        conn.execute(
            "UPDATE accounts SET balance_cents = ?, last_ok_at = ? WHERE id = ?",
            (None if balance is None else to_cents(balance), utcnow(), account_id),
        )
        conn.commit()
    conn.execute(
        "UPDATE pluggy_items SET status = ?, last_sync_at = ?, last_error = NULL WHERE item_id = ?",
        (remote_item.get("status"), utcnow(), item["item_id"]),
    )
    conn.commit()
    return inserted


def sync_all(conn: sqlite3.Connection, config: Config, http=None) -> dict:
    """A falha de uma conexão fica registrada e não impede as outras."""
    if not config.pluggy_enabled:
        raise PluggyError("Defina PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET.")
    client = PluggyClient(config.pluggy_client_id, config.pluggy_client_secret, http)
    totals = {"inserted": 0, "errors": 0}
    for item in conn.execute("SELECT * FROM pluggy_items").fetchall():
        try:
            totals["inserted"] += sync_item(conn, client, item)
        except (PluggyError, requests.RequestException, KeyError, ValueError) as exc:
            conn.rollback()
            # Exceção do requests pode citar URL; guarda só o tipo nesses casos.
            message = str(exc) if isinstance(exc, PluggyError) else f"Falha de comunicação ({type(exc).__name__})."
            log.warning("Leitura da conexão %s falhou: %s", item["label"], message)
            conn.execute("UPDATE pluggy_items SET last_error = ? WHERE item_id = ?", (message, item["item_id"]))
            conn.commit()
            totals["errors"] += 1
    return totals
