import unittest

from organizador import finance, pluggy
from organizador.config import Config
from organizador.db import connect
from organizador.finance import Incoming
from organizador.web import create_app, parse_money


def category(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


class FinanceTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.conn.execute("INSERT INTO accounts (name, kind, created_at) VALUES ('C', 'card', 'x')")

    def test_rules_and_summary_ignore_card_payment(self):
        finance.ingest(self.conn, 1, [
            Incoming("a", "2026-11-02", -5000, "IFOOD *Pizza"),
            Incoming("b", "2026-11-03", -10000, "Pagamento de fatura"),
            Incoming("c", "2026-11-04", -700, "Loja desconhecida"),
            Incoming("d", "2026-11-05", 300000, "Salário"),
            Incoming("e", "2026-11-06", 1000, "IFOOD estorno"),
        ])
        summary = finance.month_summary(self.conn, "2026-11")
        self.assertEqual(summary["spent"], 4000 + 700)  # estorno reduz; fatura fora
        self.assertEqual(summary["income"], 300000)
        self.assertEqual(summary["uncategorized"], 2)  # loja e salário

    def test_manual_category_is_kept_and_rule_applies(self):
        finance.ingest(self.conn, 1, [Incoming("a", "2026-11-02", -700, "PADARIA X"),
                                      Incoming("b", "2026-11-03", -800, "Padaria x 2")])
        mercado = category(self.conn, "Mercado")
        applied = finance.set_category(self.conn, 1, mercado, make_rule="padaria")
        self.assertEqual(applied, 1)
        finance.ingest(self.conn, 1, [Incoming("a", "2026-11-02", -700, "PADARIA X")])
        self.assertEqual(self.conn.execute("SELECT category_manual FROM transactions WHERE id = 1").fetchone()[0], 1)

    def test_pluggy_signs(self):
        card = pluggy.to_incoming({"id": "1", "date": "2026-11-02T00:00:00.000Z", "amount": 50.5}, "CREDIT", "2026-11-22")
        self.assertEqual(card.amount_cents, -5050)
        debit = pluggy.to_incoming({"id": "2", "date": "2026-11-02T00:00:00.000Z", "amount": 10, "type": "DEBIT"}, "BANK", "2026-11-22")
        self.assertEqual(debit.amount_cents, -1000)
        pending_bank = pluggy.to_incoming({"id": "3", "date": "2026-11-02T00:00:00Z", "amount": 10,
                                           "type": "DEBIT", "status": "PENDING"}, "BANK", "2026-11-22")
        self.assertIsNone(pending_bank)

    def test_parse_money(self):
        self.assertEqual(parse_money("1.234,56"), 123456)
        self.assertEqual(parse_money("R$ 230"), 23000)
        self.assertEqual(parse_money("12.5"), 1250)
        self.assertIsNone(parse_money(""))


class WebTest(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "t.db")
        self.config = Config(db_path=self.db, app_password="segredo", secret_key="x",
                             webhook_secret="abc", evolution_to="5511987654321")
        self.client = create_app(self.config).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        self.client.post("/login", data={"senha": "segredo"})
        with self.client.session_transaction() as s:
            return s["csrf"]

    def test_requires_login(self):
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_pages_render(self):
        self.login()
        for path in ("/", "/agenda", "/gastos", "/categorias", "/contas", "/conversa"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_create_item_needs_csrf(self):
        csrf = self.login()
        form = {"tipo": "conta", "titulo": "Luz", "data": "2026-12-10", "hora": "09:00",
                "repetir": "month", "a_cada": "1", "valor": "230,00"}
        self.assertEqual(self.client.post("/agenda/novo", data=form).status_code, 400)
        self.client.post("/agenda/novo", data={**form, "_csrf": csrf})
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT amount_cents FROM items").fetchone()[0], 23000)

    def test_webhook_filters_and_always_200(self):
        payload = {"event": "messages.upsert", "data": {"key": {"remoteJid": "5511987654321@s.whatsapp.net",
                   "fromMe": False, "id": "X"}, "message": {"conversation": "tomei"}}}
        self.assertEqual(self.client.post("/webhook/evolution/errado", json=payload).status_code, 200)
        stranger = {**payload, "data": {**payload["data"], "key": {**payload["data"]["key"], "remoteJid": "5511900000000@s.whatsapp.net", "id": "Y"}}}
        self.client.post("/webhook/evolution/abc", json=stranger)
        self.client.post("/webhook/evolution/abc", json=payload)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual([r[0] for r in conn.execute("SELECT message_id FROM inbox")], ["X"])


if __name__ == "__main__":
    unittest.main()
