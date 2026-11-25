"""Painel do organizador e webhook da Evolution."""
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import agenda, finance, whatsapp
from ..config import TZ, Config
from ..db import connect, set_state, utcnow

log = logging.getLogger(__name__)

LOGIN_LIMIT = 5
LOGIN_WINDOW = 15 * 60
PUBLIC = {"login", "health", "webhook", "static"}


def parse_money(raw: str) -> int | None:
    """"1.234,56", "1234,56" ou "1234.56" em centavos; vazio vira None."""
    text = (raw or "").strip().replace("R$", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise agenda.AgendaError("Valor inválido.")
    reais, _, cents = text.partition(".")
    return int(reais) * 100 + int((cents + "00")[:2])


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=config.secret_key or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    password_digest = hashlib.sha256(config.app_password.encode()).digest()
    attempts: dict[str, deque] = defaultdict(deque)

    def get_db():
        if "db" not in g:
            g.db = connect(config.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.before_request
    def guard():
        if request.endpoint in PUBLIC:
            return None
        if config.app_password and not session.get("auth"):
            return redirect(url_for("login"))
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST":
            if not hmac.compare_digest(request.form.get("_csrf", ""), session["csrf"]):
                abort(400)
        return None

    @app.context_processor
    def inject():
        return {
            "csrf": session.get("csrf", ""),
            "brl": agenda.brl,
            "kinds": agenda.KINDS,
            "units": agenda.UNITS,
            "has_password": bool(config.app_password),
            "to_local": agenda.to_local,
        }

    # ---------- sessão ----------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.app_password or session.get("auth"):
            return redirect(url_for("today"))
        if request.method == "POST":
            ip = request.remote_addr or "?"
            recent = attempts[ip]
            now = time.monotonic()
            while recent and now - recent[0] > LOGIN_WINDOW:
                recent.popleft()
            if len(recent) >= LOGIN_LIMIT:
                flash("Muitas tentativas. Aguarde 15 minutos.", "error")
                return render_template("login.html"), 429
            typed = hashlib.sha256(request.form.get("senha", "").encode()).digest()
            if hmac.compare_digest(typed, password_digest):
                attempts.pop(ip, None)
                session.clear()
                session["auth"] = True
                session["csrf"] = secrets.token_hex(16)
                session.permanent = True
                return redirect(url_for("today"))
            recent.append(now)
            flash("Senha incorreta.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login") if config.app_password else url_for("today"))

    # ---------- webhook ----------
    @app.post("/webhook/evolution/<secret>")
    def webhook(secret):
        """Sempre 200 para a Evolution; só grava mensagens privadas de texto do dono."""
        # Uma linha por chamada, sem texto nem números, para saber se a Evolution está chamando.
        if not config.webhook_enabled:
            log.warning("Webhook chamado, mas desligado: falta WEBHOOK_SECRET ou EVOLUTION_TO.")
            return {"ok": True}
        if not hmac.compare_digest(secret, config.webhook_secret):
            log.warning("Webhook chamado com segredo diferente de WEBHOOK_SECRET; ignorado.")
            return {"ok": True}
        payload = request.get_json(silent=True) or {}
        messages = whatsapp.owner_messages(payload, config.evolution_to)
        stored = whatsapp.store(get_db(), messages) if messages else 0
        data = payload.get("data") if isinstance(payload, dict) else None
        total = len(data) if isinstance(data, list) else int(isinstance(data, dict))
        log.info("Webhook: evento %s, %d mensagem(ns), %d sua(s), %d nova(s).",
                 (payload.get("event") if isinstance(payload, dict) else None) or "?",
                 total, len(messages), stored)
        return {"ok": True}

    # ---------- hoje ----------
    @app.get("/")
    def today():
        conn = get_db()
        now = datetime.now(TZ)
        month = now.strftime("%Y-%m")
        return render_template(
            "today.html",
            pending=agenda.pending(conn),
            upcoming=agenda.upcoming(conn, now, 7)[:20],
            summary=finance.month_summary(conn, month),
            now=now,
            when=lambda d: agenda.when_text(d, now),
        )

    @app.post("/ocorrencia/<int:occ_id>/<action>")
    def occurrence_action(occ_id, action):
        conn = get_db()
        now = datetime.now(TZ)
        if action == "feito":
            ok = agenda.mark_done(conn, occ_id)
        elif action == "pular":
            ok = agenda.skip(conn, occ_id)
        elif action == "adiar":
            ok = agenda.snooze(conn, occ_id, 60, now) is not None
        else:
            abort(404)
        flash("Pronto." if ok else "Essa pendência já tinha sido resolvida.", "ok" if ok else "error")
        return redirect(request.form.get("voltar") or url_for("today"))

    # ---------- agenda ----------
    @app.get("/agenda")
    def agenda_page():
        conn = get_db()
        items = conn.execute("SELECT * FROM items ORDER BY active DESC, due_at IS NULL, due_at").fetchall()
        history = conn.execute(
            "SELECT o.*, i.title, i.kind FROM occurrences o JOIN items i ON i.id = o.item_id "
            "WHERE o.status != 'pendente' ORDER BY o.done_at DESC LIMIT 30"
        ).fetchall()
        default_day = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        return render_template("agenda.html", items=items, history=history, default_day=default_day,
                               rhythms=agenda.DEFAULT_RHYTHM)

    @app.post("/agenda/novo")
    def agenda_new():
        form = request.form
        try:
            due = datetime.strptime(f"{form.get('data', '')} {form.get('hora', '')}", "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Data ou hora inválida.", "error")
            return redirect(url_for("agenda_page"))
        kind = form.get("tipo", "")
        try:
            base = agenda.DEFAULT_RHYTHM.get(kind)
            if base is None:
                raise agenda.AgendaError("Tipo inválido.")

            def minutes(name, default):
                raw = form.get(name, "").strip()
                return int(raw) if raw else default

            rhythm = agenda.Rhythm(
                minutes("antecedencia", base.remind_before_min),
                minutes("insistir", base.nag_every_min),
                minutes("max_avisos", base.max_reminders),
            )
            agenda.create_item(
                get_db(), kind=kind, title=form.get("titulo", ""), due_local=due.replace(tzinfo=TZ),
                rec_unit=form.get("repetir", "none"), rec_every=int(form.get("a_cada") or 1),
                amount_cents=parse_money(form.get("valor", "")), notes=form.get("obs", ""), rhythm=rhythm,
            )
            flash("Lembrete criado.", "ok")
        except (agenda.AgendaError, ValueError) as exc:
            flash(str(exc) or "Dados inválidos.", "error")
        return redirect(url_for("agenda_page"))

    @app.post("/agenda/<int:item_id>/ativo")
    def agenda_toggle(item_id):
        active = request.form.get("ativo") == "1"
        conn = get_db()
        row = conn.execute("SELECT due_at FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            abort(404)
        if active and row["due_at"] is None:
            flash("Esse lembrete já terminou. Crie um novo.", "error")
        else:
            agenda.set_active(conn, item_id, active)
        return redirect(url_for("agenda_page"))

    # ---------- gastos ----------
    def month_arg() -> str:
        raw = request.args.get("mes", "")
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", raw):
            return raw
        return datetime.now(TZ).strftime("%Y-%m")

    def shift(month: str, delta: int) -> str:
        moved = agenda.add_months(datetime(int(month[:4]), int(month[5:]), 1), delta)
        return moved.strftime("%Y-%m")

    @app.get("/gastos")
    def spending():
        conn = get_db()
        month = month_arg()
        only = request.args.get("filtro", "")
        query = (
            "SELECT t.*, a.name AS account, c.name AS category FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id "
            "WHERE substr(t.date, 1, 7) = ?"
        )
        params = [month]
        if only == "sem":
            query += " AND t.category_id IS NULL"
        elif only.isdigit():
            query += " AND t.category_id = ?"
            params.append(int(only))
        rows = conn.execute(query + " ORDER BY t.date DESC, t.id DESC LIMIT 500", params).fetchall()
        return render_template(
            "spending.html", month=month, prev=shift(month, -1), next=shift(month, 1), rows=rows,
            summary=finance.month_summary(conn, month), filtro=only,
            categories=conn.execute("SELECT * FROM categories ORDER BY name").fetchall(),
        )

    @app.post("/gastos/<int:tx_id>/categoria")
    def spending_category(tx_id):
        raw = request.form.get("categoria", "")
        category = int(raw) if raw.isdigit() else None
        applied = finance.set_category(get_db(), tx_id, category, request.form.get("regra", ""))
        flash(f"Categoria salva{f'; regra aplicada em mais {applied}' if applied else ''}.", "ok")
        return redirect(request.form.get("voltar") or url_for("spending"))

    @app.get("/categorias")
    def categories_page():
        conn = get_db()
        return render_template(
            "categories.html",
            categories=conn.execute("SELECT * FROM categories ORDER BY name").fetchall(),
            rules=conn.execute(
                "SELECT r.*, c.name AS category FROM rules r JOIN categories c ON c.id = r.category_id ORDER BY r.pattern"
            ).fetchall(),
        )

    @app.post("/categorias/nova")
    def category_new():
        name = request.form.get("nome", "").strip()[:60]
        if name:
            conn = get_db()
            conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
            conn.commit()
        return redirect(url_for("categories_page"))

    @app.post("/categorias/<int:cat_id>")
    def category_update(cat_id):
        try:
            limit = parse_money(request.form.get("limite", ""))
        except agenda.AgendaError:
            flash("Limite inválido.", "error")
            return redirect(url_for("categories_page"))
        conn = get_db()
        conn.execute(
            "UPDATE categories SET monthly_limit_cents = ?, ignore_in_totals = ? WHERE id = ?",
            (limit, 1 if request.form.get("ignorar") else 0, cat_id),
        )
        conn.commit()
        flash("Categoria atualizada.", "ok")
        return redirect(url_for("categories_page"))

    @app.post("/regras/nova")
    def rule_new():
        pattern = finance.normalize(request.form.get("texto", ""))
        raw = request.form.get("categoria", "")
        if pattern and raw.isdigit():
            conn = get_db()
            conn.execute(
                "INSERT INTO rules (pattern, category_id, created_at) VALUES (?,?,?) "
                "ON CONFLICT (pattern) DO UPDATE SET category_id = excluded.category_id",
                (pattern, int(raw), utcnow()),
            )
            conn.commit()
            applied = finance.apply_rules(conn)
            flash(f"Regra salva; aplicada em {applied} lançamento(s) sem categoria.", "ok")
        return redirect(url_for("categories_page"))

    @app.post("/regras/<int:rule_id>/apagar")
    def rule_delete(rule_id):
        conn = get_db()
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        conn.commit()
        flash("Regra removida (lançamentos já categorizados continuam como estão).", "ok")
        return redirect(url_for("categories_page"))

    # ---------- contas ----------
    @app.get("/contas")
    def accounts_page():
        conn = get_db()
        return render_template(
            "accounts.html",
            items=conn.execute("SELECT * FROM pluggy_items ORDER BY label").fetchall(),
            accounts=conn.execute("SELECT * FROM accounts ORDER BY name").fetchall(),
            pluggy_enabled=config.pluggy_enabled,
        )

    @app.post("/contas/nova")
    def account_new():
        item_id = request.form.get("item_id", "").strip()
        label = request.form.get("nome", "").strip()[:60] or "Banco"
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", item_id):
            flash("O identificador da conexão (item) do Pluggy tem 36 caracteres.", "error")
            return redirect(url_for("accounts_page"))
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO pluggy_items (item_id, label, created_at) VALUES (?,?,?)",
            (item_id, label, utcnow()),
        )
        set_state(conn, "pluggy_requested", "1")
        conn.commit()
        flash("Conexão adicionada. A leitura começa em instantes.", "ok")
        return redirect(url_for("accounts_page"))

    @app.post("/contas/ler")
    def account_sync():
        conn = get_db()
        set_state(conn, "pluggy_requested", "1")
        conn.commit()
        flash("Leitura pedida. O worker faz em até um minuto.", "ok")
        return redirect(url_for("accounts_page"))

    # ---------- conversa ----------
    @app.get("/conversa")
    def chat_page():
        conn = get_db()
        return render_template(
            "chat.html",
            log=conn.execute("SELECT * FROM chat_log ORDER BY id DESC LIMIT 100").fetchall(),
            problems=conn.execute(
                "SELECT * FROM inbox WHERE error IS NOT NULL ORDER BY id DESC LIMIT 20"
            ).fetchall(),
            webhook_enabled=config.webhook_enabled,
            claude_enabled=config.claude_enabled,
        )

    @app.post("/conversa/testar")
    def chat_test():
        """Simula uma mensagem do dono, sem passar pelo WhatsApp (resposta aparece no histórico)."""
        text = request.form.get("texto", "").strip()[:2000]
        if text:
            whatsapp.store(get_db(), [(f"painel-{secrets.token_hex(8)}", text)])
            flash("Mensagem enviada ao bot; a resposta aparece em alguns segundos.", "ok")
        return redirect(url_for("chat_page"))

    return app

