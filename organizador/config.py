"""Configuração lida do ambiente (.env localmente, variáveis do Easypanel em produção)."""
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

TZ = ZoneInfo("America/Sao_Paulo")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    db_path: str = "data/organizador.db"
    app_password: str = ""
    secret_key: str = ""
    secure_cookies: bool = False
    host: str = "127.0.0.1"
    port: int = 8095
    notifiers: tuple[str, ...] = ("console",)
    evolution_url: str = ""
    evolution_apikey: str = ""
    evolution_instance: str = ""
    evolution_to: str = ""
    webhook_secret: str = ""
    anthropic_api_key: str = field(default="", repr=False)
    claude_model: str = "claude-haiku-5-5"
    pluggy_client_id: str = ""
    pluggy_client_secret: str = field(default="", repr=False)
    quiet_start: int = 22
    quiet_end: int = 7

    @property
    def pluggy_enabled(self) -> bool:
        return bool(self.pluggy_client_id and self.pluggy_client_secret)

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_secret and self.evolution_to)


def load() -> Config:
    load_dotenv()
    password = os.getenv("APP_PASSWORD", "")
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if not password:
        host = "127.0.0.1"  # sem senha, o painel nunca fica exposto
    return Config(
        db_path=os.getenv("DB_PATH", "data/organizador.db").strip() or "data/organizador.db",
        app_password=password,
        secret_key=os.getenv("SECRET_KEY", ""),
        secure_cookies=os.getenv("SECURE_COOKIES", "").strip().lower() in ("1", "true", "sim"),
        host=host,
        port=_int("DASHBOARD_PORT", 8095),
        notifiers=tuple(n.strip() for n in os.getenv("NOTIFIERS", "console").split(",") if n.strip()),
        evolution_url=os.getenv("EVOLUTION_URL", "").strip(),
        evolution_apikey=os.getenv("EVOLUTION_APIKEY", "").strip(),
        evolution_instance=os.getenv("EVOLUTION_INSTANCE", "").strip(),
        evolution_to=os.getenv("EVOLUTION_TO", "").strip(),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5",
        pluggy_client_id=os.getenv("PLUGGY_CLIENT_ID", "").strip(),
        pluggy_client_secret=os.getenv("PLUGGY_CLIENT_SECRET", "").strip(),
        quiet_start=_int("QUIET_START", 22),
        quiet_end=_int("QUIET_END", 7),
    )
