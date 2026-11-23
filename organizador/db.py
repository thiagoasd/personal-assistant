"""Banco SQLite. Schema só aditivo: tabelas novas e colunas novas, nunca recriar."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- conta | remedio | compromisso | tarefa
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER,               -- contas a pagar
    due_at TEXT,                        -- próxima ocorrência (UTC ...Z); NULL quando acabou
    rec_unit TEXT NOT NULL DEFAULT 'none',  -- none | hour | day | week | month | year
    rec_every INTEGER NOT NULL DEFAULT 1,
    rec_anchor_day INTEGER,             -- dia do mês original (31 continua 31 depois de fevereiro)
    remind_before_min INTEGER NOT NULL DEFAULT 0,
    nag_every_min INTEGER NOT NULL DEFAULT 0,
    max_reminders INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'painel',  -- painel | whatsapp
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',  -- pendente | feito | pulado
    reminder_count INTEGER NOT NULL DEFAULT 0,
    last_reminded_at TEXT,
    snoozed_until TEXT,
    done_at TEXT,
    done_via TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (item_id, due_at)
);
CREATE INDEX IF NOT EXISTS occurrences_status ON occurrences(status);

-- Mensagens recebidas do dono pelo WhatsApp. Só as do dono chegam aqui.
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL,
    started_at TEXT,
    processed_at TEXT,
    reply TEXT,
    error TEXT
);
-- Conversa (texto do dono e respostas do bot), usada como memória curta da IA e no painel.
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,                 -- user | bot
    text TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    ignore_in_totals INTEGER NOT NULL DEFAULT 0,
    monthly_limit_cents INTEGER
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,       -- texto normalizado; casa se estiver contido na descrição
    category_id INTEGER NOT NULL REFERENCES categories(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pluggy_items (
    item_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT,
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- checking | card
    pluggy_account_id TEXT UNIQUE,
    pluggy_item_id TEXT,
    balance_cents INTEGER,
    last_ok_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    external_id TEXT NOT NULL,
    date TEXT NOT NULL,                 -- AAAA-MM-DD (data local)
    amount_cents INTEGER NOT NULL,      -- negativo = saída
    description TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    category_manual INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, external_id)
);
CREATE INDEX IF NOT EXISTS transactions_date ON transactions(date);
CREATE TABLE IF NOT EXISTS job_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_CATEGORIES = [
    ("Mercado", 0), ("Restaurantes e delivery", 0), ("Transporte", 0), ("Moradia", 0),
    ("Contas da casa", 0), ("Saúde", 0), ("Lazer", 0), ("Compras", 0), ("Assinaturas", 0),
    ("Educação", 0), ("Impostos e taxas", 0), ("Outros", 0), ("Receitas", 0),
    ("Pagamento de fatura", 1), ("Transferência entre contas", 1),
]

DEFAULT_RULES = [
    ("pagamento de fatura", "Pagamento de fatura"),
    ("pagamento fatura", "Pagamento de fatura"),
    ("ifood", "Restaurantes e delivery"),
    ("uber", "Transporte"),
    ("99app", "Transporte"),
    ("netflix", "Assinaturas"),
    ("spotify", "Assinaturas"),
    ("drogasil", "Saúde"),
    ("drogaria", "Saúde"),
    ("farmacia", "Saúde"),
    ("supermercado", "Mercado"),
    ("posto", "Transporte"),
]


def utcnow() -> str:
    return iso(datetime.now(timezone.utc))


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _seed(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        return
    for name, ignore in DEFAULT_CATEGORIES:
        conn.execute("INSERT INTO categories (name, ignore_in_totals) VALUES (?, ?)", (name, ignore))
    now = utcnow()
    for pattern, category in DEFAULT_RULES:
        conn.execute(
            "INSERT INTO rules (pattern, category_id, created_at) "
            "SELECT ?, id, ? FROM categories WHERE name = ?",
            (pattern, now, category),
        )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM job_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO job_state (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
