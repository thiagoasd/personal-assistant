"""Comandos: serve (painel + worker), status, sync, demo."""
import argparse
import logging
from datetime import datetime, timedelta

from . import agenda, config as config_mod, pluggy, worker
from .config import TZ
from .db import connect
from .finance import Incoming, ingest
from .db import utcnow


def cmd_serve(cfg, args) -> None:
    from waitress import serve

    from .web import create_app

    connect(cfg.db_path).close()  # cria o schema antes das duas threads
    if not args.no_worker:
        worker.start(cfg)
    port = args.port or cfg.port
    print(f"Painel em http://{cfg.host}:{port}  (worker {'desligado' if args.no_worker else 'ligado'})")
    serve(create_app(cfg), host=cfg.host, port=port, threads=4)


def cmd_status(cfg, _args) -> None:
    """Mostra o que está configurado, sem imprimir nenhum valor secreto."""
    conn = connect(cfg.db_path)
    yes = lambda v: "sim" if v else "não"  # noqa: E731
    print(f"Banco: {cfg.db_path}")
    print(f"Senha do painel: {yes(cfg.app_password)} · envio: {', '.join(cfg.notifiers)}")
    print(f"Webhook WhatsApp: {yes(cfg.webhook_enabled)} · Claude: {yes(cfg.claude_enabled)} ({cfg.claude_model})")
    print(f"Pluggy: {yes(cfg.pluggy_enabled)}")
    items = conn.execute("SELECT COUNT(*) FROM items WHERE active = 1").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM occurrences WHERE status = 'pendente'").fetchone()[0]
    txs = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"Itens ativos: {items} · pendências: {pend} · lançamentos: {txs}")


def cmd_sync(cfg, _args) -> None:
    print(pluggy.sync_all(connect(cfg.db_path), cfg))


def cmd_demo(cfg, _args) -> None:
    """Preenche um banco com dados fictícios para ver as telas. Recusa banco que já tem dados."""
    conn = connect(cfg.db_path)
    if conn.execute("SELECT 1 FROM items UNION ALL SELECT 1 FROM transactions LIMIT 1").fetchone():
        raise SystemExit("O banco já tem dados; use outro DB_PATH para a demonstração.")
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    day = now.replace(hour=9, minute=0)
    agenda.create_item(conn, kind="conta", title="Conta de luz", due_local=day + timedelta(days=1),
                       rec_unit="month", amount_cents=23_450)
    agenda.create_item(conn, kind="conta", title="Internet", due_local=day - timedelta(hours=2),
                       rec_unit="month", amount_cents=11_990)
    agenda.create_item(conn, kind="remedio", title="Vitamina D", due_local=now - timedelta(minutes=10),
                       rec_unit="day", notes="1 cápsula depois do café")
    agenda.create_item(conn, kind="compromisso", title="Dentista", due_local=day + timedelta(days=3, hours=6))
    agenda.create_item(conn, kind="tarefa", title="Renovar CNH", due_local=day + timedelta(days=5))
    agenda.materialize(conn, now)
    conn.execute("INSERT INTO accounts (name, kind, created_at) VALUES ('Demo · Conta', 'checking', ?)", (utcnow(),))
    conn.execute("INSERT INTO accounts (name, kind, created_at) VALUES ('Demo · Cartão', 'card', ?)", (utcnow(),))
    month = now.strftime("%Y-%m")
    rows = [
        ("1", "02", -32_480, "Supermercado Pão Doce"), ("2", "03", -6_590, "IFOOD *Restaurante"),
        ("3", "05", -2_340, "Uber *Trip"), ("4", "06", -5_590, "Netflix.com"),
        ("5", "07", -18_900, "Drogaria Bom Preço"), ("6", "08", -9_870, "Loja de roupas XYZ"),
        ("7", "09", -15_000, "Posto Shell"), ("8", "10", -4_200, "Padaria Central"),
    ]
    ingest(conn, 2, [Incoming(f"demo-{i}", f"{month}-{d}", v, t) for i, d, v, t in rows])
    ingest(conn, 1, [
        Incoming("demo-in", f"{month}-01", 650_000, "Transferência recebida - Salário"),
        Incoming("demo-fat", f"{month}-04", -98_770, "Pagamento de fatura"),
    ])
    conn.execute("UPDATE categories SET monthly_limit_cents = 30000 WHERE name = 'Mercado'")
    conn.commit()
    print(f"Demonstração criada em {cfg.db_path}.")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="organizador")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="painel e worker")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-worker", action="store_true", help="só o painel (sem avisos nem leituras)")
    sub.add_parser("status", help="o que está configurado")
    sub.add_parser("sync", help="lê o Pluggy agora")
    sub.add_parser("demo", help="banco de demonstração com dados fictícios")
    args = parser.parse_args(argv)
    cfg = config_mod.load()
    {"serve": cmd_serve, "status": cmd_status, "sync": cmd_sync, "demo": cmd_demo}[args.command](cfg, args)
