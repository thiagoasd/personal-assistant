"""Envio de mensagens. Copiado do meli-seller-bot (`melibot/notifiers.py`), com destino único."""
import logging
from typing import Protocol

import requests

from .config import Config

log = logging.getLogger(__name__)


def split_message(text: str, max_len: int) -> list[str]:
    """Quebra em partes de até max_len, preferindo quebrar entre linhas."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> None: ...


class ConsoleNotifier:
    name = "console"

    def send(self, text: str) -> None:
        print(f"\n--- WhatsApp ---\n{text}\n----------------", flush=True)


class EvolutionWhatsApp:
    """WhatsApp pela Evolution API v2, sempre para o número do dono."""

    name = "evolution"
    MAX_LEN = 4000

    def __init__(self, base_url: str, apikey: str, instance: str, to: str):
        if not (base_url and apikey and instance and to):
            raise RuntimeError("EVOLUTION_URL, EVOLUTION_APIKEY, EVOLUTION_INSTANCE e EVOLUTION_TO são obrigatórios")
        self.base_url = base_url.rstrip("/")
        self.headers = {"apikey": apikey}
        self.instance = instance
        self.to = to

    def send(self, text: str) -> None:
        for chunk in split_message(text, self.MAX_LEN):
            resp = requests.post(
                f"{self.base_url}/message/sendText/{self.instance}",
                json={"number": self.to, "text": chunk},
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()


def build_notifiers(config: Config) -> list[Notifier]:
    notifiers: list[Notifier] = []
    for name in config.notifiers:
        if name == "console":
            notifiers.append(ConsoleNotifier())
        elif name == "evolution":
            notifiers.append(EvolutionWhatsApp(
                config.evolution_url, config.evolution_apikey, config.evolution_instance, config.evolution_to
            ))
        else:
            raise RuntimeError(f"Notificador desconhecido: {name}")
    if not notifiers:
        raise RuntimeError("Defina ao menos um canal em NOTIFIERS")
    return notifiers


class DeliveryError(Exception):
    """Nenhum canal conseguiu entregar. A mensagem não traz URL nem chave."""


def deliver(notifiers: list[Notifier], text: str) -> None:
    """Entregar em um canal basta: tentar de novo duplicaria a mensagem nos que funcionaram."""
    failures = []
    for notifier in notifiers:
        try:
            notifier.send(text)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            failures.append(f"{notifier.name}: HTTP {status}")
        except Exception as exc:  # um canal fora do ar não pode impedir os outros
            failures.append(f"{notifier.name}: {type(exc).__name__}")
    if len(failures) == len(notifiers):
        raise DeliveryError("; ".join(failures))
    if failures:
        log.warning("Canais com falha: %s", "; ".join(failures))
