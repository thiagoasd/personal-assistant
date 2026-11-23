"""Agenda: itens com repetição, ocorrências pendentes e quando avisar.

Um item (conta, remédio, compromisso, tarefa) tem o horário da próxima vez (`due_at`).
Quando chega a hora do aviso (horário menos a antecedência), vira uma ocorrência pendente e o
item avança para a próxima repetição. A ocorrência fica pendente até ser marcada feita ou pulada.
"""
import calendar
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import TZ
from .db import iso, parse_iso, utcnow

KINDS = {
    "conta": "Conta a pagar",
    "remedio": "Remédio",
    "compromisso": "Compromisso",
    "tarefa": "Tarefa",
}
UNITS = {
    "none": "Não repete",
    "hour": "hora(s)",
    "day": "dia(s)",
    "week": "semana(s)",
    "month": "mês(es)",
    "year": "ano(s)",
}


@dataclass(frozen=True)
class Rhythm:
    remind_before_min: int
    nag_every_min: int
    max_reminders: int


DEFAULT_RHYTHM = {
    "conta": Rhythm(24 * 60, 6 * 60, 6),
    "remedio": Rhythm(0, 30, 4),
    "compromisso": Rhythm(60, 0, 1),
    "tarefa": Rhythm(0, 4 * 60, 3),
}


class AgendaError(ValueError):
    pass


def add_months(moment: datetime, months: int) -> datetime:
    """Mesmo dia nos meses seguintes; dia 31 vira o último dia dos meses mais curtos."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def next_due(local: datetime, unit: str, every: int, anchor_day: int | None = None) -> datetime | None:
    """Próxima repetição em horário local. `anchor_day` preserva o dia 31 depois de fevereiro."""
    every = max(1, every)
    if unit == "none":
        return None
    if unit == "hour":
        return local + timedelta(hours=every)
    if unit == "day":
        return local + timedelta(days=every)
    if unit == "week":
        return local + timedelta(weeks=every)
    if unit in ("month", "year"):
        moved = add_months(local, every * (12 if unit == "year" else 1))
        if anchor_day:
            last = calendar.monthrange(moved.year, moved.month)[1]
            moved = moved.replace(day=min(anchor_day, last))
        return moved
    raise AgendaError(f"Repetição desconhecida: {unit}")


def to_local(stamp: str) -> datetime:
    return parse_iso(stamp).astimezone(TZ)


def create_item(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    due_local: datetime,
    rec_unit: str = "none",
    rec_every: int = 1,
    amount_cents: int | None = None,
    notes: str = "",
    rhythm: Rhythm | None = None,
    source: str = "painel",
) -> int:
    if kind not in KINDS:
        raise AgendaError("Tipo inválido.")
    if rec_unit not in UNITS:
        raise AgendaError("Repetição inválida.")
    title = title.strip()
    if not title:
        raise AgendaError("Informe o nome.")
    if rec_every < 1 or rec_every > 1000:
        raise AgendaError("O intervalo da repetição deve ficar entre 1 e 1000.")
    if amount_cents is not None and amount_cents < 0:
        raise AgendaError("O valor não pode ser negativo.")
    if due_local.tzinfo is None:
        due_local = due_local.replace(tzinfo=TZ)
    rhythm = rhythm or DEFAULT_RHYTHM[kind]
    cursor = conn.execute(
        "INSERT INTO items (kind, title, notes, amount_cents, due_at, rec_unit, rec_every, rec_anchor_day, "
        "remind_before_min, nag_every_min, max_reminders, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, title[:200], notes.strip()[:1000], amount_cents, iso(due_local), rec_unit, rec_every,
         due_local.astimezone(TZ).day if rec_unit in ("month", "year") else None,
         max(0, rhythm.remind_before_min), max(0, rhythm.nag_every_min),
         max(1, rhythm.max_reminders), source, utcnow()),
    )
    conn.commit()
    return cursor.lastrowid


def set_active(conn: sqlite3.Connection, item_id: int, active: bool) -> bool:
    changed = conn.execute(
        "UPDATE items SET active = ? WHERE id = ?", (1 if active else 0, item_id)
    ).rowcount
    conn.commit()
    return bool(changed)


def materialize(conn: sqlite3.Connection, now: datetime) -> int:
    """Cria as ocorrências que chegaram na hora do aviso e avança os itens. Devolve quantas.

    Se várias repetições já passaram (servidor fora do ar), só a mais recente vira ocorrência.
    """
    created = 0
    rows = conn.execute("SELECT * FROM items WHERE active = 1 AND due_at IS NOT NULL").fetchall()
    for item in rows:
        before = timedelta(minutes=item["remind_before_min"])
        due = to_local(item["due_at"])
        if due - before > now:
            continue
        anchor = item["rec_anchor_day"]
        following = next_due(due, item["rec_unit"], item["rec_every"], anchor)
        # Pula as repetições que já foram substituídas por uma mais nova.
        while following is not None and following - before <= now:
            due = following
            following = next_due(due, item["rec_unit"], item["rec_every"], anchor)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO occurrences (item_id, due_at, created_at) VALUES (?,?,?)",
            (item["id"], iso(due), utcnow()),
        ).rowcount
        created += inserted
        conn.execute(
            "UPDATE items SET due_at = ?, active = ? WHERE id = ?",
            (iso(following) if following else None, 1 if following else 0, item["id"]),
        )
    conn.commit()
    return created


def in_quiet_hours(local: datetime, start: int, end: int) -> bool:
    hour = local.hour
    if start == end:
        return False
    if start > end:  # atravessa a meia-noite (22h às 7h)
        return hour >= start or hour < end
    return start <= hour < end


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT o.*, i.kind, i.title, i.amount_cents, i.notes, i.nag_every_min, i.max_reminders "
        "FROM occurrences o JOIN items i ON i.id = o.item_id "
        "WHERE o.status = 'pendente' ORDER BY o.due_at, o.id"
    ).fetchall()


def due_reminders(conn: sqlite3.Connection, now: datetime, quiet_start: int, quiet_end: int) -> list[sqlite3.Row]:
    """Ocorrências que devem receber aviso agora.

    O primeiro aviso sai sempre (inclusive remédio às 23h). As insistências respeitam o
    silêncio da noite e param no máximo de avisos. Adiar marca um novo aviso para depois.
    """
    quiet = in_quiet_hours(now.astimezone(TZ), quiet_start, quiet_end)
    result = []
    for occ in pending(conn):
        if occ["snoozed_until"]:
            if parse_iso(occ["snoozed_until"]) <= now:
                result.append(occ)
            continue
        if occ["last_reminded_at"] is None:
            result.append(occ)
            continue
        if quiet or occ["nag_every_min"] <= 0 or occ["reminder_count"] >= occ["max_reminders"]:
            continue
        if now - parse_iso(occ["last_reminded_at"]) >= timedelta(minutes=occ["nag_every_min"]):
            result.append(occ)
    return result


def record_reminder(conn: sqlite3.Connection, occ_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE occurrences SET reminder_count = reminder_count + 1, last_reminded_at = ?, "
        "snoozed_until = NULL WHERE id = ?",
        (iso(now), occ_id),
    )
    conn.commit()


def _finish(conn, occ_id: int, status: str, via: str) -> bool:
    changed = conn.execute(
        "UPDATE occurrences SET status = ?, done_at = ?, done_via = ?, snoozed_until = NULL "
        "WHERE id = ? AND status = 'pendente'",
        (status, utcnow(), via, occ_id),
    ).rowcount
    conn.commit()
    return bool(changed)


def mark_done(conn: sqlite3.Connection, occ_id: int, via: str = "painel") -> bool:
    return _finish(conn, occ_id, "feito", via)


def skip(conn: sqlite3.Connection, occ_id: int, via: str = "painel") -> bool:
    return _finish(conn, occ_id, "pulado", via)


def snooze(conn: sqlite3.Connection, occ_id: int, minutes: int, now: datetime) -> datetime | None:
    if minutes < 1 or minutes > 60 * 24 * 30:
        raise AgendaError("Adie entre 1 minuto e 30 dias.")
    until = now + timedelta(minutes=minutes)
    changed = conn.execute(
        "UPDATE occurrences SET snoozed_until = ? WHERE id = ? AND status = 'pendente'",
        (iso(until), occ_id),
    ).rowcount
    conn.commit()
    return until if changed else None


def upcoming(conn: sqlite3.Connection, now: datetime, days: int) -> list[dict]:
    """Próximas ocorrências dos itens ativos nos próximos `days` dias (projeção, sem gravar)."""
    limit = now + timedelta(days=days)
    result = []
    for item in conn.execute("SELECT * FROM items WHERE active = 1 AND due_at IS NOT NULL ORDER BY due_at"):
        due = to_local(item["due_at"])
        anchor = item["rec_anchor_day"]
        count = 0
        while due is not None and due <= limit and count < 50:
            result.append({"item": item, "due": due})
            due = next_due(due, item["rec_unit"], item["rec_every"], anchor)
            count += 1
    result.sort(key=lambda entry: entry["due"])
    return result


def brl(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(cents), 100)
    return f"{sign}R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"


def when_text(local: datetime, now: datetime) -> str:
    today = now.astimezone(TZ).date()
    day = local.date()
    if day == today:
        label = "hoje"
    elif day == today + timedelta(days=1):
        label = "amanhã"
    elif day == today - timedelta(days=1):
        label = "ontem"
    else:
        label = local.strftime("%d/%m")
    return f"{label} às {local.strftime('%H:%M')}"


@dataclass(frozen=True)
class Menu:
    """Opções numeradas do aviso: 1 resolve, 2 adia, 3 pula."""
    done: str
    snooze_label: str
    snooze_minutes: int
    skip: str


MENUS = {
    "conta": Menu("Paguei", "Adiar 1 h", 60, "Pular"),
    "remedio": Menu("Tomei", "Adiar 1 h", 60, "Pular"),
    "compromisso": Menu("Ok", "Lembrar em 15 min", 15, "Não vou"),
    "tarefa": Menu("Feito", "Adiar 1 h", 60, "Pular"),
}


def menu_line(kind: str) -> str:
    menu = MENUS[kind]
    return f"Responda: 1 {menu.done} · 2 {menu.snooze_label} · 3 {menu.skip}"


def reminder_text(occ: sqlite3.Row, now: datetime) -> str:
    due = to_local(occ["due_at"])
    late = due < now
    if occ["kind"] == "conta":
        head = f"Pagar: {occ['title']}"
        if occ["amount_cents"] is not None:
            head += f" ({brl(occ['amount_cents'])})"
        timing = ("venceu " if late else "vence ") + when_text(due, now)
    elif occ["kind"] == "remedio":
        head = f"Hora do remédio: {occ['title']}"
        timing = when_text(due, now)
    elif occ["kind"] == "compromisso":
        head = f"Compromisso: {occ['title']}"
        timing = when_text(due, now)
    else:
        head = f"Tarefa: {occ['title']}"
        timing = ("era para " if late else "para ") + when_text(due, now)
    lines = [f"🔔 {head}", f"{timing} · #{occ['id']}"]
    if occ["notes"]:
        lines.append(occ["notes"])
    lines.append(menu_line(occ["kind"]))
    return "\n".join(lines)


def describe_occurrence(occ: sqlite3.Row, now: datetime) -> dict:
    """Versão em dicionário para a IA e para os comandos simples."""
    due = to_local(occ["due_at"])
    return {
        "ocorrencia": occ["id"],
        "tipo": KINDS[occ["kind"]],
        "titulo": occ["title"],
        "valor": brl(occ["amount_cents"]) or None,
        "quando": due.strftime("%d/%m/%Y %H:%M"),
        "atrasado": due < now,
        "avisos_enviados": occ["reminder_count"],
    }
