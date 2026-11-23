import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from organizador import agenda, assistant, whatsapp
from organizador.config import TZ, Config
from organizador.db import connect

OWNER = "5511987654321"
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=TZ)


class MenuTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def remind(self, kind, title, when):
        agenda.create_item(self.conn, kind=kind, title=title, due_local=when)
        agenda.materialize(self.conn, when)
        occ = [o for o in agenda.pending(self.conn) if o["title"] == title][0]
        agenda.record_reminder(self.conn, occ["id"], when)
        return occ["id"]

    def test_one_marks_latest_reminder(self):
        occ = self.remind("conta", "Luz", NOW)
        reply = assistant.menu_reply(self.conn, "1", NOW)
        self.assertEqual(reply, f"✅ Paguei: Luz (#{occ}).")
        self.assertEqual(agenda.pending(self.conn), [])

    def test_two_snoozes_by_kind(self):
        self.remind("compromisso", "Dentista", NOW)
        self.assertIn("10:15", assistant.menu_reply(self.conn, "2", NOW))

    def test_three_skips(self):
        occ = self.remind("remedio", "Vitamina", NOW)
        assistant.menu_reply(self.conn, "3", NOW)
        status = self.conn.execute("SELECT status FROM occurrences WHERE id = ?", (occ,)).fetchone()[0]
        self.assertEqual(status, "pulado")

    def test_ambiguous_when_reminders_arrive_together(self):
        first = self.remind("conta", "Luz", NOW)
        second = self.remind("remedio", "Vitamina", NOW + timedelta(minutes=5))
        reply = assistant.menu_reply(self.conn, "1", NOW + timedelta(minutes=6))
        self.assertIn("vários avisos", reply)
        self.assertEqual(len(agenda.pending(self.conn)), 2)
        assistant.menu_reply(self.conn, f"1 #{first}", NOW)
        self.assertEqual([o["id"] for o in agenda.pending(self.conn)], [second])

    def test_older_reminder_does_not_block(self):
        self.remind("conta", "Luz", NOW - timedelta(hours=3))
        occ = self.remind("remedio", "Vitamina", NOW)
        self.assertIn(f"#{occ}", assistant.menu_reply(self.conn, "1", NOW))

    def test_quoted_reply_and_resolved(self):
        old = self.remind("conta", "Luz", NOW)
        self.remind("remedio", "Vitamina", NOW + timedelta(minutes=1))
        text = f"1 {whatsapp.QUOTE_MARK.format(ref=old)}"
        self.assertIn("Luz", assistant.menu_reply(self.conn, text, NOW))
        self.assertIn("já estava resolvido", assistant.menu_reply(self.conn, text, NOW))

    def test_other_text_is_not_menu(self):
        self.remind("conta", "Luz", NOW)
        for text in ("10", "1 e 2", "paguei", "12h"):
            self.assertIsNone(assistant.menu_reply(self.conn, text, NOW), text)

    def test_menu_skips_claude(self):
        self.remind("conta", "Luz", NOW)
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: self.fail("não deveria chamar a IA")))
        assistant.handle(self.conn, Config(), "1", now=NOW, client=client)
        self.assertEqual(agenda.pending(self.conn), [])


class QuotedMessageTest(unittest.TestCase):
    def payload(self, quoted):
        return {"event": "messages.upsert", "data": {
            "key": {"remoteJid": f"{OWNER}@s.whatsapp.net", "fromMe": False, "id": "Q1"},
            "message": {"extendedTextMessage": {"text": "1", "contextInfo": {
                "quotedMessage": {"conversation": quoted}}}},
        }}

    def test_reference_from_quoted_reminder(self):
        found = whatsapp.owner_messages(self.payload("🔔 Pagar: Luz\nvence hoje às 09:00 · #7\nResponda: 1 Paguei"), OWNER)
        self.assertEqual(found, [("Q1", "1 [respondendo ao aviso #7]")])

    def test_list_with_many_refs_is_ignored(self):
        found = whatsapp.owner_messages(self.payload("#7 Luz\n#8 Vitamina"), OWNER)
        self.assertEqual(found, [("Q1", "1")])


if __name__ == "__main__":
    unittest.main()
