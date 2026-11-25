import unittest
from datetime import datetime, timedelta

from organizador import agenda
from organizador.config import TZ
from organizador.db import connect


def at(day, hour=9, minute=0, month=11, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class RecurrenceTest(unittest.TestCase):
    def test_monthly_keeps_day_31_after_february(self):
        jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=TZ)
        feb = agenda.next_due(jan31, "month", 1, 31)
        self.assertEqual(feb.day, 28)
        self.assertEqual(agenda.next_due(feb, "month", 1, 31).day, 31)

    def test_none_does_not_repeat(self):
        self.assertIsNone(agenda.next_due(at(1), "none", 1))

    def test_every_n_hours(self):
        self.assertEqual(agenda.next_due(at(1, 8), "hour", 8), at(1, 16))


class MaterializeTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_reminder_before_due_and_item_advances(self):
        item = agenda.create_item(self.conn, kind="conta", title="Luz", due_local=at(10),
                                  rec_unit="month", amount_cents=23000)
        self.assertEqual(agenda.materialize(self.conn, at(9, 8, 59)), 0)
        self.assertEqual(agenda.materialize(self.conn, at(9, 9, 0)), 1)  # 1 dia antes
        row = self.conn.execute("SELECT due_at FROM items WHERE id = ?", (item,)).fetchone()
        self.assertEqual(agenda.to_local(row["due_at"]), at(10, month=12))
        self.assertEqual(len(agenda.pending(self.conn)), 1)

    def test_only_latest_missed_repetition(self):
        agenda.create_item(self.conn, kind="remedio", title="Vitamina", due_local=at(1, 8), rec_unit="day")
        self.assertEqual(agenda.materialize(self.conn, at(5, 9)), 1)
        occ = agenda.pending(self.conn)[0]
        self.assertEqual(agenda.to_local(occ["due_at"]), at(5, 8))

    def test_fixed_day_already_passed_starts_next_month(self):
        item = agenda.create_item(self.conn, kind="conta", title="Água", due_local=at(10),
                                  rec_unit="month", now=at(25, 10))
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()
        self.assertEqual(agenda.to_local(row["due_at"]), at(10, month=12))
        self.assertEqual(agenda.materialize(self.conn, at(25, 10)), 0)  # nada de aviso "vencido"
        self.assertEqual(agenda.repeat_text(row["rec_unit"], row["rec_every"], row["rec_anchor_day"]), "todo dia 10")

    def test_day_31_created_late_keeps_anchor(self):
        item = agenda.create_item(self.conn, kind="conta", title="Aluguel", due_local=at(31, month=1),
                                  rec_unit="month", now=at(5, month=2))
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()
        self.assertEqual(agenda.to_local(row["due_at"]).day, 28)
        self.assertEqual(row["rec_anchor_day"], 31)

    def test_repeat_text(self):
        self.assertEqual(agenda.repeat_text("none", 1), "não")
        self.assertEqual(agenda.repeat_text("month", 2, 5), "a cada 2 meses, no dia 5")
        self.assertEqual(agenda.repeat_text("hour", 8), "a cada 8 hora(s)")

    def test_month_calendar_mixes_history_and_projection(self):
        now = at(25, 10)
        agenda.create_item(self.conn, kind="remedio", title="Vitamina", due_local=at(3, 8))
        agenda.materialize(self.conn, at(3, 8))
        occ = agenda.pending(self.conn)[0]["id"]
        agenda.mark_done(self.conn, occ)
        agenda.create_item(self.conn, kind="conta", title="Água", due_local=at(10), rec_unit="month", now=now)

        def entries(weeks):
            return {(d["date"].day, d["date"].month, e["title"], e["status"])
                    for w in weeks for d in w for e in d["entries"]}

        nov = agenda.month_calendar(self.conn, 2026, 11, now)
        self.assertEqual(nov[0][0]["date"].weekday(), 0)  # começa na segunda
        self.assertTrue(all(len(w) == 7 for w in nov))
        self.assertEqual(entries(nov), {(3, 11, "Vitamina", "feito")})
        self.assertEqual([d["date"].day for w in nov for d in w if d["today"]], [25])
        dec = agenda.month_calendar(self.conn, 2026, 12, now)
        self.assertEqual(entries(dec), {(10, 12, "Água", "previsto")})
        self.assertIn((10, 3, "Água", "previsto"), entries(agenda.month_calendar(self.conn, 2027, 3, now)))

    def test_single_item_ends(self):
        item = agenda.create_item(self.conn, kind="compromisso", title="Dentista", due_local=at(3, 15))
        agenda.materialize(self.conn, at(3, 14))
        row = self.conn.execute("SELECT active, due_at FROM items WHERE id = ?", (item,)).fetchone()
        self.assertEqual((row["active"], row["due_at"]), (0, None))
        self.assertEqual(agenda.materialize(self.conn, at(3, 16)), 0)


class RemindersTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        agenda.create_item(self.conn, kind="remedio", title="Remédio", due_local=at(1, 21, 50), rec_unit="day")
        agenda.materialize(self.conn, at(1, 21, 50))

    def due(self, now):
        return agenda.due_reminders(self.conn, now, 22, 7)

    def test_first_reminder_then_nag_then_quiet(self):
        first = self.due(at(1, 21, 50))
        self.assertEqual(len(first), 1)
        agenda.record_reminder(self.conn, first[0]["id"], at(1, 21, 50))
        self.assertEqual(self.due(at(1, 22, 10)), [])  # 20 min depois, ainda não
        self.assertEqual(self.due(at(1, 22, 30)), [])  # 40 min depois, mas silêncio
        self.assertEqual(len(self.due(at(2, 7, 0))), 1)

    def test_first_reminder_ignores_quiet_hours(self):
        conn = connect(":memory:")
        agenda.create_item(conn, kind="remedio", title="Noite", due_local=at(1, 23), rec_unit="day")
        agenda.materialize(conn, at(1, 23))
        self.assertEqual(len(agenda.due_reminders(conn, at(1, 23), 22, 7)), 1)

    def test_max_reminders_and_done(self):
        occ = self.due(at(1, 21, 50))[0]
        for i in range(4):
            agenda.record_reminder(self.conn, occ["id"], at(2, 8) + timedelta(hours=i))
        self.assertEqual(self.due(at(2, 20)), [])
        self.assertTrue(agenda.mark_done(self.conn, occ["id"], via="teste"))
        self.assertFalse(agenda.mark_done(self.conn, occ["id"]))

    def test_snooze(self):
        occ = self.due(at(1, 21, 50))[0]
        agenda.record_reminder(self.conn, occ["id"], at(1, 21, 50))
        agenda.snooze(self.conn, occ["id"], 120, at(1, 21, 55))
        self.assertEqual(self.due(at(1, 23, 0)), [])
        self.assertEqual(len(self.due(at(1, 23, 55))), 1)  # adiado sai mesmo no silêncio

    def test_text_has_reference(self):
        occ = self.due(at(1, 21, 50))[0]
        text = agenda.reminder_text(occ, at(1, 21, 50))
        self.assertIn(f"#{occ['id']}", text)
        self.assertIn("1 Tomei · 2 Adiar 1 h · 3 Pular", text)

    def test_brl(self):
        self.assertEqual(agenda.brl(123456), "R$ 1.234,56")
        self.assertEqual(agenda.brl(-5), "-R$ 0,05")


if __name__ == "__main__":
    unittest.main()
