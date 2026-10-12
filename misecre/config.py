from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field

CATEGORIES_ORDER = ("trabajo", "personal", "ocio")


class SttCfg(BaseModel):
    provider: str = "local"                # local | groq | openai (los dos últimos usan una API en la nube)
    api_model: str = ""                    # vacío = el recomendado del proveedor
    api_timeout_s: float = 30.0
    fallback_local: bool = True            # si la API falla, probar con el modelo local
    model: str = "small"
    language: str = "es"
    device: str = "auto"
    compute_type: str = "int8"
    beam_size: int = 5                     # 1 = más rápido, algo menos preciso
    preload: bool = True                   # cargar el modelo al arrancar (no en el primer audio)


class ClassifierCfg(BaseModel):
    min_score: float = 1.0
    min_margin: float = 0.5


class CalendarCfg(BaseModel):
    enabled: bool = False
    skip_categories: list[str] = Field(default_factory=list)   # categorías que NO van a Google (sí avisan)
    credentials_file: str = "credentials.json"
    names: dict[str, str] = Field(default_factory=dict)


class TelegramCfg(BaseModel):
    allowed_chat_ids: list[int] = Field(default_factory=list)


class ApiCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_ips: list[str] = Field(default_factory=list)   # IPs/CIDR permitidas; vacío = todas
    trust_proxy: bool = False                               # leer la IP real de X-Forwarded-For
    trusted_proxies: int = 1                                # cuántos proxies tuyos hay delante (nginx = 1)


class AiCfg(BaseModel):
    enabled: bool = False
    model: str = "claude-haiku-5-5"
    max_calls_per_day: int = 100           # tope de la clasificación de respaldo
    timeout_s: float = 30.0
    min_confidence: float = 0.6
    agent: bool = True                     # chat con agente operador (si ai.enabled)
    mode: str = "rules_first"              # rules_first | ai_first | rules_only (editable con /modo)
    agent_max_calls_per_day: int = 300     # llamadas a la API del agente por día
    agent_max_rounds: int = 6              # idas y vueltas con herramientas por mensaje
    agent_history_messages: int = 16       # mensajes de contexto que recuerda
    agent_history_hours: int = 12          # y hasta cuántas horas atrás


class CategoryPolicy(BaseModel):
    before: list[int] = Field(default_factory=list)   # minutos antes del evento
    digest_only: bool = False                          # no empuja avisos; solo aparece en el resumen


class NotificationsCfg(BaseModel):
    quiet_hours: str = "23:00-07:00"
    allday_at: str = "09:00"                           # hora del aviso para lo de todo el día
    channels: list[str] = Field(default_factory=lambda: ["telegram", "desktop"])
    snooze_minutes: int = 10
    categories: dict[str, CategoryPolicy] = Field(default_factory=lambda: {
        "trabajo": CategoryPolicy(before=[15]),
        "personal": CategoryPolicy(before=[60, 10]),
        "ocio": CategoryPolicy(before=[], digest_only=True),
    })


class WeeklyCfg(BaseModel):
    day: str = "domingo"
    at: str = "20:00"


class MonthlyCfg(BaseModel):
    day: int = 1
    at: str = "08:00"


class DigestsCfg(BaseModel):
    enabled: bool = True
    daily: str = "08:00"
    periodic: list[str] = Field(default_factory=list)     # horas "HH:MM" de un resumen de cómo está todo
    periodic_window_min: int = 60                         # tolerancia si la PC prendió tarde
    periodic_skip_empty: bool = True                      # no molestar si no hay nada para mostrar
    weekly: WeeklyCfg = WeeklyCfg()
    monthly: MonthlyCfg = MonthlyCfg()


class SyncCfg(BaseModel):
    enabled: bool = True
    interval_min: int = 5
    import_calendar_events: bool = True                # eventos creados a mano en Google => capturas
    import_past_days: int = 1
    silence_google_reminders: bool = True              # los avisos los emite misecre, no Google


class LearningCfg(BaseModel):
    min_count: int = 2                     # correcciones tuyas para SUGERIR una regla
    auto_apply: bool = True                # aprende solo de las decisiones de la IA (reversible: /regla quitar)
    agent_min_count: int = 2               # veces que la IA debe decidir igual para aprenderla


class Rules(BaseModel):
    categories: dict[str, list[str | dict[str, float]]]
    event_words: list[str] = Field(default_factory=list)
    task_words: list[str] = Field(default_factory=list)
    drop_prefixes: list[str] = Field(default_factory=list)


class Config(BaseModel):
    timezone: str = "America/Argentina/Buenos_Aires"
    data_dir: Path = Path("~/.misecre")
    default_event_minutes: int = 60
    stt: SttCfg = SttCfg()
    classifier: ClassifierCfg = ClassifierCfg()
    calendar: CalendarCfg = CalendarCfg()
    telegram: TelegramCfg = TelegramCfg()
    api: ApiCfg = ApiCfg()
    ai: AiCfg = AiCfg()
    notifications: NotificationsCfg = NotificationsCfg()
    digests: DigestsCfg = DigestsCfg()
    sync: SyncCfg = SyncCfg()
    learning: LearningCfg = LearningCfg()
    rules: Rules
    unknown_keys: list[str] = Field(default_factory=list, exclude=True)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def category_names(self) -> list[str]:
        return list(self.rules.categories)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "misecre.sqlite3"

    @property
    def user_rules_path(self) -> Path:
        return self.data_dir / "rules.user.yaml"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


# Claves de versiones anteriores: se ignoran sin ruido (con aviso de que ya no hacen nada).
LEGACY_KEYS = {"vault", "sync.adopt_notes", "digests.daily_note_folder", "digests.note_refresh_at"}


def expand_dotted(raw: dict) -> dict:
    """`ai.enabled: true` (una línea) -> `ai: {enabled: true}`. En YAML eso NO anida solo."""
    out: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            value = expand_dotted(value)
        if isinstance(key, str) and "." in key:
            head, _, rest = key.partition(".")
            node = out.setdefault(head, {})
            if not isinstance(node, dict):
                continue
            sub = expand_dotted({rest: value})
            for k, v in sub.items():
                if isinstance(v, dict) and isinstance(node.get(k), dict):
                    node[k].update(v)
                else:
                    node[k] = v
        else:
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            else:
                out[key] = value
    return out


def unknown_keys(raw: dict, model: type[BaseModel] = None, prefix: str = "") -> list[str]:
    """Claves del YAML que el programa no conoce (typos): se avisan en vez de ignorarse en silencio."""
    model = model or Config
    out: list[str] = []
    fields = model.model_fields
    for key, value in raw.items():
        full = f"{prefix}{key}"
        if key not in fields:
            if full not in LEGACY_KEYS and not (prefix and any(k.startswith(full) for k in LEGACY_KEYS)):
                out.append(full)
            continue
        ann = fields[key].annotation
        if isinstance(value, dict) and isinstance(ann, type) and issubclass(ann, BaseModel):
            out += unknown_keys(value, ann, full + ".")
    return out


def _candidates() -> list[Path]:
    project = Path(__file__).resolve().parent.parent
    return [Path.cwd() / "config" / "config.yaml", project / "config" / "config.yaml",
            Path.home() / ".misecre" / "config.yaml"]


def find_config(path: str | Path | None = None) -> Path:
    """Encuentra config.yaml desde CUALQUIER carpeta: -c / MISECRE_CONFIG, luego ./config, luego la carpeta
    del proyecto y por último ~/.misecre."""
    explicit = path or os.environ.get("MISECRE_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"No existe el archivo de configuración indicado: {p}")
        return p
    for c in _candidates():
        if c.exists():
            return c
    raise FileNotFoundError(
        "No encuentro config.yaml. Busqué en:\n" + "\n".join(f"  - {c}" for c in _candidates())
        + "\nCreá uno con: cp config/config.example.yaml config/config.yaml  (o indicá -c RUTA)")


def load_config(path: str | Path | None = None) -> Config:
    path = find_config(path)
    raw = expand_dotted(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    rules_path = path.parent / raw.pop("rules_file", "rules.yaml")
    raw["rules"] = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules_raw = raw.pop("rules", None)
    unknown = unknown_keys(raw)
    raw["rules"] = rules_raw
    cfg = Config.model_validate(raw)
    cfg.unknown_keys = unknown
    cfg.data_dir = cfg.data_dir.expanduser()
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":                        # base de datos y tokens: solo para tu usuario
        try:
            cfg.data_dir.chmod(0o700)
        except OSError:
            pass
    return cfg
