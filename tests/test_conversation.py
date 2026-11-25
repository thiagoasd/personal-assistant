import unittest
from datetime import datetime
from types import SimpleNamespace

from organizador import agenda, assistant, whatsapp, worker
from organizador.config import TZ, Config
from organizador.db import connect

OWNER = "5511987654321"
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=TZ)


def upsert(jid, text, msg_id="A1", from_me=False, **key_extra):
    return {"event": "messages.upsert", "data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id, **key_extra},
        "message": {"conversation": text},
    }}


class WhatsAppParseTest(unittest.TestCase):
    def test_owner_private_message(self):
        self.assertEqual(whatsapp.owner_messages(upsert(f"{OWNER}@s.whatsapp.net", "oi"), OWNER), [("A1", "oi")])

    def test_ninth_digit_variant(self):
        self.assertEqual(len(whatsapp.owner_messages(upsert("551187654321@s.whatsapp.net", "oi"), OWNER)), 1)

    def test_lid_with_alt(self):
        payload = upsert("123456789@lid", "oi", remoteJidAlt=f"{OWNER}@s.whatsapp.net")
        self.assertEqual(len(whatsapp.owner_messages(payload, OWNER)), 1)

    def test_drops_others_groups_and_own(self):
        self.assertEqual(whatsapp.owner_messages(upsert("5511900000000@s.whatsapp.net", "oi"), OWNER), [])
        self.assertEqual(whatsapp.owner_messages(upsert("1203630@g.us", "oi", participant=f"{OWNER}@s.whatsapp.net"), OWNER), [])
        self.assertEqual(whatsapp.owner_messages(upsert(f"{OWNER}@s.whatsapp.net", "oi", from_me=True), OWNER), [])
        self.assertEqual(whatsapp.owner_messages({"event": "connection.update"}, OWNER), [])

    def test_discard_reasons_without_numbers(self):
        cases = [
            (upsert("5511900000000@s.whatsapp.net", "oi"), "outro número"),
            (upsert("1203630@g.us", "oi"), "grupo"),
            (upsert(f"{OWNER}@s.whatsapp.net", "oi", from_me=True), "próprio número"),
            (upsert("123456789@lid", "oi", participant="987@lid"), "campos em key: participant, remoteJid"),
            (upsert(f"{OWNER}@s.whatsapp.net", ""), "sem texto"),
        ]
        for payload, expected in cases:
            reasons = []
            self.assertEqual(whatsapp.owner_messages(payload, OWNER, reasons), [])
            self.assertEqual(len(reasons), 1, expected)
            self.assertIn(expected, reasons[0])
            self.assertNotRegex(reasons[0], r"\d{3}")


class SimpleCommandsTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        agenda.create_item(self.conn, kind="remedio", title="Vitamina", due_local=NOW, rec_unit="day")
        agenda.materialize(self.conn, NOW)
        self.occ = agenda.pending(self.conn)[0]["id"]
        agenda.record_reminder(self.conn, self.occ, NOW)

    def test_done_marks_latest_reminded(self):
        reply = assistant.simple_commands(self.conn, "Tomei", NOW)
        self.assertIn("Vitamina", reply)
        self.assertEqual(agenda.pending(self.conn), [])

    def test_snooze_with_hours(self):
        reply = assistant.simple_commands(self.conn, "adiar 2h", NOW)
        self.assertIn("12:00", reply)

    def test_unknown_shows_help(self):
        self.assertEqual(assistant.simple_commands(self.conn, "bom dia", NOW), assistant.HELP)


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


class FakeClaude:
    """Devolve respostas prontas e guarda o que recebeu."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ClaudeLoopTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        agenda.create_item(self.conn, kind="conta", title="Luz", due_local=NOW, amount_cents=23000)
        agenda.materialize(self.conn, NOW)
        self.occ = agenda.pending(self.conn)[0]["id"]

    def test_marks_done_via_tool(self):
        fake = FakeClaude([
            SimpleNamespace(stop_reason="tool_use", content=[
                block("tool_use", id="t1", name="marcar_feito", input={"ocorrencias": [self.occ]}),
            ]),
            SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Marquei a Luz como paga.")]),
        ])
        reply = assistant.handle(self.conn, Config(), "paguei a luz", now=NOW, client=fake)
        self.assertEqual(reply, "Marquei a Luz como paga.")
        self.assertEqual(agenda.pending(self.conn), [])
        second = fake.calls[1]["messages"]
        result = second[-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertIn(str(self.occ), result["content"])
        self.assertIn("Luz", fake.calls[0]["messages"][0]["content"][0]["text"])  # contexto com pendentes
        logged = self.conn.execute("SELECT role FROM chat_log ORDER BY id").fetchall()
        self.assertEqual([r["role"] for r in logged], ["user", "bot"])

    def test_create_reminder_validates_and_reports_error(self):
        fake = FakeClaude([
            SimpleNamespace(stop_reason="tool_use", content=[block("tool_use", id="t1", name="criar_lembrete", input={
                "tipo": "compromisso", "titulo": "Dentista", "data": "2026-11-27", "hora": "25:00",
                "repetir": "none", "a_cada": 1, "valor_centavos": 0, "observacao": "",
            })]),
            SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Hora inválida, qual horário?")]),
        ])
        assistant.handle(self.conn, Config(), "dentista sexta", now=NOW, client=fake)
        result = fake.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_ai_failure_falls_back(self):
        class Broken:
            messages = SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("fora")))

        agenda.record_reminder(self.conn, self.occ, NOW)
        reply = assistant.handle(self.conn, Config(), "paguei", now=NOW, client=Broken())
        self.assertIn("Luz", reply)


class InboxWorkerTest(unittest.TestCase):
    def test_processes_once_and_replies(self):
        conn = connect(":memory:")
        whatsapp.store(conn, [("M1", "pendentes"), ("M1", "pendentes")])
        sent = []
        notifier = SimpleNamespace(name="fake", send=sent.append)
        self.assertEqual(worker.process_inbox(conn, Config(), [notifier], now=NOW), 1)
        self.assertEqual(worker.process_inbox(conn, Config(), [notifier], now=NOW), 0)
        self.assertEqual(sent, ["Nada pendente."])

    def test_reminder_not_recorded_when_delivery_fails(self):
        conn = connect(":memory:")
        agenda.create_item(conn, kind="tarefa", title="CNH", due_local=NOW)

        def fail(_text):
            raise RuntimeError("fora")

        notifier = SimpleNamespace(name="fake", send=fail)
        self.assertEqual(worker.send_reminders(conn, Config(), [notifier], now=NOW), 0)
        self.assertIsNone(agenda.pending(conn)[0]["last_reminded_at"])


if __name__ == "__main__":
    unittest.main()
