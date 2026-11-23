"""Worker único: responde mensagens, cria ocorrências, manda avisos e lê o Pluggy, em sequência."""
import logging
import threading
from datetime import datetime, timedelta

from . import agenda, assistant, pluggy
from .config import TZ, Config
from .db import connect, get_state, iso, parse_iso, set_state, utcnow
from .notifiers import DeliveryError, build_notifiers, deliver

log = logging.getLogger(__name__)

TICK_SECONDS = 15
PLUGGY_EVERY = timedelta(hours=6)
MAX_BATCH = 10  # mais avisos que isso de uma vez viram uma mensagem só


def process_inbox(conn, config: Config, notifiers, client=None, now: datetime | None = None) -> int:
    """Responde as mensagens novas, uma vez cada.

    A mensagem é marcada como iniciada antes de chamar a IA. Se o processo cair no meio, ela
    não é reprocessada (a ação pode já ter sido feita); fica com erro e aparece no painel.
    """
    conn.execute(
        "UPDATE inbox SET error = 'Interrompida antes de terminar; confira no painel.', processed_at = ? "
        "WHERE started_at IS NOT NULL AND processed_at IS NULL AND started_at < ?",
        (utcnow(), iso(datetime.now(TZ) - timedelta(minutes=10))),
    )
    conn.commit()
    handled = 0
    for msg in conn.execute(
        "SELECT * FROM inbox WHERE started_at IS NULL ORDER BY id LIMIT 20"
    ).fetchall():
        conn.execute("UPDATE inbox SET started_at = ? WHERE id = ?", (utcnow(), msg["id"]))
        conn.commit()
        reply = assistant.handle(conn, config, msg["text"], now=now, client=client)
        error = None
        try:
            deliver(notifiers, reply)
        except DeliveryError as exc:
            error = f"Resposta não entregue: {exc}"
        conn.execute(
            "UPDATE inbox SET processed_at = ?, reply = ?, error = ? WHERE id = ?",
            (utcnow(), reply, error, msg["id"]),
        )
        conn.commit()
        handled += 1
    return handled


def send_reminders(conn, config: Config, notifiers, now: datetime | None = None) -> int:
    now = now or datetime.now(TZ)
    agenda.materialize(conn, now)
    due = agenda.due_reminders(conn, now, config.quiet_start, config.quiet_end)
    if not due:
        return 0
    if len(due) > MAX_BATCH:
        text = f"🔔 Você tem {len(due)} pendências:\n" + "\n".join(
            f"#{o['id']} {o['title']}" for o in due
        ) + "\nResponda \"pendentes\" para ver tudo."
        batches = [(due, text)]
    else:
        batches = [([o], agenda.reminder_text(o, now)) for o in due]
    sent = 0
    for occs, text in batches:
        try:
            deliver(notifiers, text)
        except DeliveryError as exc:
            log.warning("Aviso não entregue (%s); tenta de novo no próximo ciclo.", exc)
            continue
        for occ in occs:
            agenda.record_reminder(conn, occ["id"], now)
        assistant.log_bot_message(conn, text)
        sent += len(occs)
    return sent


def maybe_sync_pluggy(conn, config: Config, now: datetime | None = None) -> bool:
    if not config.pluggy_enabled:
        return False
    now = now or datetime.now(TZ)
    requested = get_state(conn, "pluggy_requested")
    last = get_state(conn, "pluggy_last_run")
    if not requested and last and now - parse_iso(last) < PLUGGY_EVERY:
        return False
    set_state(conn, "pluggy_last_run", iso(now))
    set_state(conn, "pluggy_requested", None)
    conn.commit()
    try:
        totals = pluggy.sync_all(conn, config)
        log.info("Pluggy: %s lançamentos novos, %s conexões com erro.", totals["inserted"], totals["errors"])
    except pluggy.PluggyError as exc:
        log.warning("Pluggy: %s", exc)
    return True


def run_once(conn, config: Config, notifiers) -> None:
    process_inbox(conn, config, notifiers)
    send_reminders(conn, config, notifiers)
    maybe_sync_pluggy(conn, config)


def start(config: Config) -> threading.Thread:
    notifiers = build_notifiers(config)
    stop = threading.Event()

    def loop():
        conn = connect(config.db_path)
        while not stop.is_set():
            try:
                run_once(conn, config, notifiers)
            except Exception:  # o worker nunca pode morrer por um erro isolado
                log.exception("Erro no ciclo do worker")
                conn.rollback()
            stop.wait(TICK_SECONDS)

    thread = threading.Thread(target=loop, name="worker", daemon=True)
    thread.start()
    return thread
