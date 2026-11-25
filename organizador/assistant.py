"""Conversa pelo WhatsApp: entende o que o dono escreveu e age sobre a agenda local.

Com ANTHROPIC_API_KEY, o Claude interpreta o texto livre e chama ferramentas que só mexem no
banco local (marcar feito, adiar, pular, criar e desativar lembrete, ler agenda e gastos).
Nada apaga dados nem mexe em dinheiro. Sem a chave, vale um conjunto de comandos simples.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from . import agenda, finance
from .config import TZ, Config
from .db import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
HISTORY_MESSAGES = 12
HISTORY_HOURS = 24

SYSTEM_PROMPT = """Você é o assistente pessoal do dono deste número no WhatsApp. Você cuida da agenda dele \
(contas a pagar, remédios, compromissos e tarefas) e responde perguntas sobre os gastos.

Como agir:
- Os avisos que você mandou têm um número no fim (ex.: "#12"): é o número da ocorrência. Quando \
ele disser que fez algo ("paguei", "tomei", "feito", "já fui"), descubra qual ocorrência pendente \
combina com a mensagem e chame marcar_feito. Se só houver uma pendente que combine, marque sem \
perguntar. Se houver mais de uma possível, pergunte qual, citando os números. \
"[respondendo ao aviso #N]" no fim da mensagem significa que ele respondeu citando o aviso #N.
- "Adia", "depois", "daqui a pouco" significam adiar. Sem tempo dito, adie 30 minutos.
- Para criar lembrete, entenda tipo, nome, data, hora e repetição. Se faltar a data ou a hora e \
não der para deduzir com segurança, pergunte antes de criar. Remédio sem hora não pode ser criado.
- Conta em dia fixo ("água todo dia 10"): repetir month, a_cada 1, data = dia 10 deste mês (se \
já passou, o sistema começa no mês seguinte). Conta sem hora dita: use 09:00 sem perguntar. Na \
confirmação, diga a primeira data e a repetição devolvidas pela ferramenta.
- Para desativar um lembrete, use listar_lembretes para achar o número do item e confirme o nome \
na resposta. Desativar pode ser desfeito no painel.
- Nunca invente pendências, valores ou gastos: use as ferramentas.
- Você não paga contas, não transfere dinheiro e não acessa o banco: só lê o que foi importado.

Respostas: português do Brasil, curtas (uma a três linhas), em texto simples de WhatsApp, sem \
markdown de títulos ou tabelas. Valores no formato R$ 1.234,56. Ao marcar, adiar, criar ou \
desativar algo, confirme exatamente o que foi feito."""


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("listar_pendentes", "Lista as ocorrências pendentes (avisadas e ainda não feitas).", {}),
    _tool("marcar_feito", "Marca uma ou mais ocorrências pendentes como feitas.", {
        "ocorrencias": {"type": "array", "items": {"type": "integer"}, "description": "Números das ocorrências (#)."},
    }),
    _tool("pular", "Marca ocorrências como puladas (não serão feitas desta vez; a repetição continua).", {
        "ocorrencias": {"type": "array", "items": {"type": "integer"}},
    }),
    _tool("adiar", "Adia o próximo aviso de uma ocorrência pendente.", {
        "ocorrencia": {"type": "integer"},
        "minutos": {"type": "integer", "description": "Entre 1 e 43200."},
    }),
    _tool("criar_lembrete", "Cria um item na agenda, que gera avisos no WhatsApp.", {
        "tipo": {"type": "string", "enum": list(agenda.KINDS)},
        "titulo": {"type": "string"},
        "data": {"type": "string", "description": "AAAA-MM-DD, horário de Brasília."},
        "hora": {"type": "string", "description": "HH:MM, 24 h."},
        "repetir": {"type": "string", "enum": list(agenda.UNITS),
                    "description": "none = não repete; hour/day/week/month/year. month repete todo mês no dia da data."},
        "a_cada": {"type": "integer", "description": "Intervalo da repetição (1 = toda vez). Use 1 se não repete."},
        "valor_centavos": {"type": "integer", "description": "Valor da conta em centavos; 0 se não houver."},
        "observacao": {"type": "string", "description": "Texto extra no aviso; vazio se não houver."},
    }),
    _tool("listar_lembretes", "Lista os itens ativos da agenda com o número de cada um.", {}),
    _tool("desativar_lembrete", "Desativa um item da agenda (para de avisar). Reversível no painel.", {
        "item": {"type": "integer"},
    }),
    _tool("listar_agenda", "Próximos compromissos, contas, remédios e tarefas.", {
        "dias": {"type": "integer", "description": "Quantos dias à frente (1 a 60)."},
    }),
    _tool("resumo_gastos", "Total gasto e gastos por categoria num mês.", {
        "mes": {"type": "string", "description": "AAAA-MM."},
    }),
]


class ToolError(ValueError):
    pass


def run_tool(conn: sqlite3.Connection, name: str, args: dict, now: datetime) -> dict:
    """Executa uma ferramenta e devolve um resultado para a IA. Valida tudo de novo aqui."""
    if name == "listar_pendentes":
        return {"pendentes": [agenda.describe_occurrence(o, now) for o in agenda.pending(conn)]}
    if name in ("marcar_feito", "pular"):
        ids = [int(i) for i in args.get("ocorrencias", [])][:20]
        action = agenda.mark_done if name == "marcar_feito" else agenda.skip
        done = [i for i in ids if action(conn, i, via="whatsapp")]
        return {"alteradas": done, "nao_encontradas_ou_ja_resolvidas": [i for i in ids if i not in done]}
    if name == "adiar":
        until = agenda.snooze(conn, int(args["ocorrencia"]), int(args["minutos"]), now)
        if until is None:
            raise ToolError("Ocorrência não encontrada ou já resolvida.")
        return {"proximo_aviso": until.astimezone(TZ).strftime("%d/%m %H:%M")}
    if name == "criar_lembrete":
        try:
            due = datetime.strptime(f"{args['data']} {args['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError as exc:
            raise ToolError("Data ou hora inválida.") from exc
        if due < now - timedelta(minutes=5) and args["repetir"] == "none":
            raise ToolError("Essa data e hora já passaram.")
        amount = int(args.get("valor_centavos") or 0) or None
        item_id = agenda.create_item(
            conn, kind=args["tipo"], title=args["titulo"], due_local=due, rec_unit=args["repetir"],
            rec_every=int(args.get("a_cada") or 1), amount_cents=amount,
            notes=args.get("observacao", ""), source="whatsapp", now=now,
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return {"item": item_id, "primeira_vez": agenda.to_local(row["due_at"]).strftime("%d/%m/%Y %H:%M"),
                "repete": agenda.repeat_text(row["rec_unit"], row["rec_every"], row["rec_anchor_day"])}
    if name == "listar_lembretes":
        rows = conn.execute("SELECT * FROM items WHERE active = 1 ORDER BY due_at").fetchall()
        return {"itens": [{
            "item": r["id"], "tipo": agenda.KINDS[r["kind"]], "titulo": r["title"],
            "proxima_vez": agenda.to_local(r["due_at"]).strftime("%d/%m/%Y %H:%M"),
            "repete": agenda.repeat_text(r["rec_unit"], r["rec_every"], r["rec_anchor_day"]),
        } for r in rows]}
    if name == "desativar_lembrete":
        row = conn.execute("SELECT title FROM items WHERE id = ?", (int(args["item"]),)).fetchone()
        if row is None or not agenda.set_active(conn, int(args["item"]), False):
            raise ToolError("Item não encontrado.")
        return {"desativado": row["title"]}
    if name == "listar_agenda":
        days = min(max(int(args.get("dias") or 7), 1), 60)
        return {"agenda": [{
            "quando": e["due"].strftime("%d/%m %H:%M"), "tipo": agenda.KINDS[e["item"]["kind"]],
            "titulo": e["item"]["title"], "valor": agenda.brl(e["item"]["amount_cents"]) or None,
        } for e in agenda.upcoming(conn, now, days)][:40]}
    if name == "resumo_gastos":
        month = str(args.get("mes", ""))
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ToolError("Mês deve ser AAAA-MM.")
        summary = finance.month_summary(conn, month)
        return {
            "mes": month, "total_gasto": agenda.brl(summary["spent"]), "entradas": agenda.brl(summary["income"]),
            "lancamentos_sem_categoria": summary["uncategorized"],
            "por_categoria": [{"categoria": c["name"], "gasto": agenda.brl(c["spent"]),
                               "limite": agenda.brl(c["limit"]) or None, "passou_do_limite": c["over"]}
                              for c in summary["categories"]],
            "observacao": "Só inclui o que já foi importado das contas pelo Pluggy.",
        }
    raise ToolError(f"Ferramenta desconhecida: {name}")


def _history(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    since = iso(now - timedelta(hours=HISTORY_HOURS))
    rows = conn.execute(
        "SELECT role, text FROM chat_log WHERE at >= ? ORDER BY id DESC LIMIT ?", (since, HISTORY_MESSAGES)
    ).fetchall()
    messages = [{"role": "user" if r["role"] == "user" else "assistant", "content": r["text"]} for r in reversed(rows)]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _context(conn: sqlite3.Connection, now: datetime) -> str:
    local = now.astimezone(TZ)
    weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    pend = [agenda.describe_occurrence(o, now) for o in agenda.pending(conn)][:15]
    return (
        f"[Contexto do sistema] Agora: {weekdays[local.weekday()]}, {local.strftime('%d/%m/%Y %H:%M')} "
        f"(Brasília). Pendentes: {json.dumps(pend, ensure_ascii=False)}"
    )


def ask_claude(conn: sqlite3.Connection, config: Config, text: str, now: datetime, client=None) -> str:
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=config.anthropic_api_key, max_retries=2, timeout=60)
    messages = _history(conn, now)
    messages.append({"role": "user", "content": [
        {"type": "text", "text": _context(conn, now)},
        {"type": "text", "text": text},
    ]})
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=config.claude_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            output_config={"effort": "low"},
            messages=messages,
        )
        if response.stop_reason == "refusal":
            return "Não consegui tratar essa mensagem. Tente de outro jeito ou use o painel."
        if response.stop_reason != "tool_use":
            reply = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return reply or "Certo."
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                output = run_tool(conn, block.name, dict(block.input), now)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, ensure_ascii=False)})
            except (ToolError, agenda.AgendaError, KeyError, TypeError, ValueError) as exc:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(exc) or type(exc).__name__, "is_error": True})
        messages.append({"role": "user", "content": results})
    return "Fiz o que consegui, mas a conversa ficou longa demais. Confira no painel."


# ---------- comandos simples (sem IA) ----------

HELP = (
    "Responda o aviso com 1, 2 ou 3 (ou \"1 #12\"). Comandos: \"feito\" (ou \"feito #12\"), \"pular #12\", \"adiar 30min\" / \"adiar 2h\", "
    "\"pendentes\", \"agenda\". Para cadastrar, use o painel."
)
DONE_WORDS = ("feito", "feita", "ok", "paguei", "pago", "tomei", "tomado", "fiz", "pronto", "ja fiz", "já fiz")


AMBIGUOUS_WINDOW = timedelta(minutes=30)
QUOTE_PATTERN = re.compile(r"\[respondendo ao aviso #\d+\]")


class Ambiguous(Exception):
    """Mais de um aviso recente: a resposta sem número não diz a qual se refere."""

    def __init__(self, options: list[sqlite3.Row]):
        super().__init__("ambíguo")
        self.options = options

    def reply(self, example: str) -> str:
        lines = [f"#{o['id']} {o['title']}" for o in self.options]
        return ("Chegaram vários avisos juntos. Responda em cima do aviso, ou mande "
                f"\"{example} #{self.options[0]['id']}\":\n" + "\n".join(lines))


def _latest_reminded(conn: sqlite3.Connection) -> sqlite3.Row:
    """Pendência do último aviso enviado. Se outros avisos saíram até 30 min antes, é ambíguo."""
    rows = conn.execute(
        "SELECT o.*, i.title, i.kind FROM occurrences o JOIN items i ON i.id = o.item_id "
        "WHERE o.status = 'pendente' AND o.last_reminded_at IS NOT NULL "
        "ORDER BY o.last_reminded_at DESC, o.id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return None
    newest = parse_iso(rows[0]["last_reminded_at"])
    close = [r for r in rows if newest - parse_iso(r["last_reminded_at"]) <= AMBIGUOUS_WINDOW]
    if len(close) > 1:
        raise Ambiguous(close)
    return rows[0]


def menu_reply(conn: sqlite3.Connection, text: str, now: datetime) -> str | None:
    """Resposta "1", "2" ou "3" ao menu do aviso, com ou sem "#12". None se não for isso.

    Tratada sem IA: é a resposta mais comum e precisa ser exata.
    """
    lowered = finance.normalize(text)
    refs = {int(n) for n in re.findall(r"#\s*(\d+)", lowered)}
    rest = re.sub(r"#\s*\d+", "", QUOTE_PATTERN.sub("", lowered)).strip(" .!")
    if rest not in ("1", "2", "3"):
        return None
    if len(refs) > 1:
        return "Mande um aviso por vez, por exemplo \"1 #12\"."
    if refs:
        occ = conn.execute(
            "SELECT o.*, i.title, i.kind FROM occurrences o JOIN items i ON i.id = o.item_id WHERE o.id = ?",
            (refs.pop(),),
        ).fetchone()
        if occ is None:
            return "Não achei esse aviso."
        if occ["status"] != "pendente":
            return f"{occ['title']} (#{occ['id']}) já estava resolvido."
    else:
        try:
            occ = _latest_reminded(conn)
        except Ambiguous as exc:
            return exc.reply(rest)
        if occ is None:
            return "Não há aviso pendente para responder."
    menu = agenda.MENUS[occ["kind"]]
    label = f"{occ['title']} (#{occ['id']})"
    if rest == "1":
        agenda.mark_done(conn, occ["id"], via="whatsapp")
        return f"✅ {menu.done}: {label}."
    if rest == "2":
        until = agenda.snooze(conn, occ["id"], menu.snooze_minutes, now)
        return f"⏰ {label}: aviso de novo às {until.astimezone(TZ).strftime('%H:%M')}."
    agenda.skip(conn, occ["id"], via="whatsapp")
    return f"↷ {menu.skip}: {label}. A repetição continua."


def simple_commands(conn: sqlite3.Connection, text: str, now: datetime) -> str:
    lowered = finance.normalize(text)
    refs = [int(n) for n in re.findall(r"#\s*(\d+)", lowered)]
    first = lowered.split(" ")[0] if lowered else ""

    def target() -> list[int]:
        if refs:
            return refs
        latest = _latest_reminded(conn)
        return [latest["id"]] if latest else []

    try:
        return _simple(conn, lowered, first, target, now)
    except Ambiguous as exc:
        return exc.reply(first or "feito")


def _simple(conn, lowered, first, target, now) -> str:
    if first in ("pendentes", "pendente"):
        items = agenda.pending(conn)
        if not items:
            return "Nada pendente."
        return "\n".join(f"#{o['id']} {o['title']} ({agenda.when_text(agenda.to_local(o['due_at']), now)})" for o in items[:15])
    if first == "agenda":
        entries = agenda.upcoming(conn, now, 7)
        if not entries:
            return "Nada nos próximos 7 dias."
        return "\n".join(f"{e['due'].strftime('%d/%m %H:%M')} {e['item']['title']}" for e in entries[:20])
    if first in ("pular", "pula"):
        ids = target()
        done = [i for i in ids if agenda.skip(conn, i, via="whatsapp")]
        return f"Pulado: {', '.join(f'#{i}' for i in done)}." if done else "Não achei pendência para pular."
    if first in ("adiar", "adia"):
        match = re.search(r"(\d+)\s*(min|m|h)", lowered)
        minutes = 30 if not match else int(match.group(1)) * (60 if match.group(2) == "h" else 1)
        ids = target()
        if not ids:
            return "Não achei pendência para adiar."
        until = agenda.snooze(conn, ids[0], minutes, now)
        return f"Adiado #{ids[0]} para {until.astimezone(TZ).strftime('%H:%M')}." if until else "Essa pendência já foi resolvida."
    if any(lowered == w or lowered.startswith(w + " ") for w in DONE_WORDS):
        ids = target()
        done = [i for i in ids if agenda.mark_done(conn, i, via="whatsapp")]
        if not done:
            return "Não achei pendência para marcar."
        rows = conn.execute(
            f"SELECT o.id, i.title FROM occurrences o JOIN items i ON i.id = o.item_id WHERE o.id IN ({','.join('?' * len(done))})",
            done,
        ).fetchall()
        return "Marcado como feito: " + "; ".join(f"{r['title']} (#{r['id']})" for r in rows) + "."
    return HELP


def handle(conn: sqlite3.Connection, config: Config, text: str, now: datetime | None = None, client=None) -> str:
    """Responde uma mensagem do dono e grava a conversa."""
    now = now or datetime.now(TZ)
    reply = menu_reply(conn, text, now)
    if reply is not None:
        pass
    elif config.claude_enabled or client is not None:
        try:
            reply = ask_claude(conn, config, text, now, client)
        except Exception as exc:  # IA fora do ar: cai para os comandos simples
            log.warning("Falha na IA (%s); usando comandos simples.", type(exc).__name__)
            reply = simple_commands(conn, text, now)
            if reply == HELP:
                reply = "A IA está indisponível agora. " + HELP
    else:
        reply = simple_commands(conn, text, now)
    stamp = utcnow()
    conn.execute("INSERT INTO chat_log (role, text, at) VALUES ('user', ?, ?)", (text, stamp))
    conn.execute("INSERT INTO chat_log (role, text, at) VALUES ('bot', ?, ?)", (reply, stamp))
    conn.commit()
    return reply


def log_bot_message(conn: sqlite3.Connection, text: str) -> None:
    """Avisos enviados também entram na memória curta, para a IA saber do que se trata."""
    conn.execute("INSERT INTO chat_log (role, text, at) VALUES ('bot', ?, ?)", (text, utcnow()))
    conn.commit()

