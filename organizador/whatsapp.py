"""Recebimento de mensagens pelo webhook da Evolution API.

O número da loja recebe mensagens de clientes e de grupos. Daqui só passa mensagem de texto de
`EVOLUTION_TO`, que pode ser:
- o número do dono: vale só a conversa privada com ele;
- o id de um grupo (`...@g.us`): vale só esse grupo, de qualquer participante menos o próprio bot.
O resto é descartado sem ser gravado.
"""
import re
import sqlite3

from .db import utcnow

# Anexado ao texto quando o dono responde citando um aviso; a IA e o menu leem o número daqui.
QUOTE_MARK = "[respondendo ao aviso #{ref}]"


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", (value or "").split("@")[0].split(":")[0])


def _canonical(number: str) -> str:
    """Celular brasileiro com ou sem o 9 extra (55 + DDD + 9 + 8 dígitos) vira a mesma chave."""
    if number.startswith("55") and len(number) == 13 and number[4] == "9":
        return number[:4] + number[5:]
    return number


def same_phone(a: str, b: str) -> bool:
    a, b = digits(a), digits(b)
    return bool(a and b) and _canonical(a) == _canonical(b)


def _text(message: dict) -> str:
    if not isinstance(message, dict):
        return ""
    if isinstance(message.get("conversation"), str):
        return message["conversation"]
    extended = message.get("extendedTextMessage") or {}
    if isinstance(extended.get("text"), str):
        return extended["text"]
    return ""


def _quoted_ref(entry: dict) -> int | None:
    """Número do aviso (#12) da mensagem citada, quando o dono responde em cima de um aviso.

    Só vale se a mensagem citada tiver exatamente um número (uma lista de pendências não conta).
    """
    message = entry.get("message") or {}
    info = entry.get("contextInfo") or (message.get("extendedTextMessage") or {}).get("contextInfo") or {}
    quoted = _text(info.get("quotedMessage") or {})
    refs = set(re.findall(r"#(\d+)", quoted))
    return int(refs.pop()) if len(refs) == 1 else None


def owner_messages(payload: dict, owner: str, discarded: list[str] | None = None) -> list[tuple[str, str]]:
    """(id da mensagem, texto) das mensagens do dono neste evento. Aceita objeto ou lista em `data`.

    Se `discarded` for uma lista, recebe o motivo de cada mensagem descartada (sem números nem
    texto), para o log mostrar por que nada foi aceito.
    """
    note = discarded.append if discarded is not None else (lambda _: None)
    if not isinstance(payload, dict) or not owner:
        return []
    event = str(payload.get("event", "")).lower().replace("_", ".")
    if event and event != "messages.upsert":
        return []
    data = payload.get("data")
    entries = data if isinstance(data, list) else [data]
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key") or {}
        if key.get("fromMe"):
            note("enviada pelo próprio número da instância")
            continue
        jid = str(key.get("remoteJid") or "")
        if owner.endswith("@g.us"):
            # Modo grupo: só vale o grupo configurado, de qualquer participante (menos o próprio bot).
            if jid != owner:
                note(f"outro grupo ou conversa (id: {jid})" if jid.endswith("@g.us")
                     else "conversa fora do grupo configurado")
                continue
        elif jid.endswith("@g.us"):
            note(f"grupo (id do grupo: {jid})")
            continue
        if jid.endswith("@broadcast") or jid.endswith("@newsletter"):
            note("lista ou canal")
            continue
        if not owner.endswith("@g.us"):
            # Com o identificador @lid, o número real vem em remoteJidAlt/senderPn.
            candidates = [jid, key.get("remoteJidAlt"), key.get("senderPn"), entry.get("senderPn")]
            phones = [c for c in candidates if c and "@lid" not in str(c)]
            if not phones:
                fields = ", ".join(sorted(k for k in key if k not in ("id", "fromMe"))) or "nenhum"
                note(f"identificador @lid sem número de telefone (campos em key: {fields})")
                continue
            if not any(same_phone(c, owner) for c in phones):
                note("outro número (cliente ou EVOLUTION_TO diferente)")
                continue
        text = _text(entry.get("message") or {}).strip()[:2000]
        message_id = str(key.get("id") or "")
        ref = _quoted_ref(entry)
        if text and ref is not None and f"#{ref}" not in text.replace(" ", ""):
            text = f"{text} {QUOTE_MARK.format(ref=ref)}"
        if text and message_id:
            found.append((message_id, text))
        else:
            note("sua, mas sem texto (áudio, foto, figurinha…)")
    return found


def store(conn: sqlite3.Connection, messages: list[tuple[str, str]]) -> int:
    stored = 0
    for message_id, text in messages:
        stored += conn.execute(
            "INSERT OR IGNORE INTO inbox (message_id, text, received_at) VALUES (?,?,?)",
            (message_id, text, utcnow()),
        ).rowcount
    conn.commit()
    return stored
