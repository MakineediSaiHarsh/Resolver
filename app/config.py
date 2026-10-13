from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


def flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).lower()
    if raw not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return raw in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    allow_chat_fallback: bool = True
    gemini_timeout_seconds: float = 12
    checkout_failure_seconds: float = 2
    bank_authorisation_seconds: float = 35
    payment_capture_seconds: float = 37
    order_confirmation_seconds: float = 38
    escalation_seconds: float = 45
    session_ttl_seconds: float = 1800
    max_sessions: int = 200
    max_chat_messages: int = 30
    max_gemini_concurrency: int = 4
    session_creates_per_minute: int = 12
    cors_origins: tuple[str, ...] = ()
    frontend_dir: str = ""
    storage_backend: str = "auto"
    supabase_url: str = ""
    supabase_secret_key: str = field(default="", repr=False)
    supabase_timeout_seconds: float = 8
    require_shared_storage: bool = False

    @property
    def resolved_storage(self):
        if self.storage_backend == 'auto':
            return 'supabase' if self.supabase_url or self.supabase_secret_key or self.require_shared_storage else 'memory'
        return self.storage_backend

    def __post_init__(self):
        if not (0 < self.checkout_failure_seconds < self.bank_authorisation_seconds
                < self.payment_capture_seconds < self.order_confirmation_seconds
                < self.escalation_seconds):
            raise ValueError("Demo timings must increase: failure < authorisation < capture < order confirmation < escalation")
        if self.session_ttl_seconds <= self.escalation_seconds:
            raise ValueError("Session lifetime must exceed the demo timeline")
        if min(self.max_sessions, self.max_chat_messages, self.max_gemini_concurrency,
               self.session_creates_per_minute) < 1:
            raise ValueError("Capacity settings must be positive")
        if self.gemini_timeout_seconds <= 0:
            raise ValueError("Gemini timeout must be positive")
        if self.storage_backend not in {'auto', 'memory', 'supabase'}:
            raise ValueError('STORAGE_BACKEND must be auto, memory or supabase')
        if self.require_shared_storage and self.storage_backend == 'memory':
            raise ValueError('Vercel requires shared storage; configure Supabase')
        if self.supabase_timeout_seconds <= 0:
            raise ValueError('Supabase timeout must be positive')
        if self.supabase_url:
            url = urlsplit(self.supabase_url)
            if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError('SUPABASE_URL must be the HTTPS project URL without credentials or query parameters')
        if self.supabase_secret_key.startswith(('sb_publishable_', 'anon')):
            raise ValueError('Use a server-only Supabase secret key, not a publishable/anon key')

    @classmethod
    def from_env(cls):
        floats = {name: float(os.getenv(name.upper(), default)) for name, default in {
            "gemini_timeout_seconds": 12, "checkout_failure_seconds": 2,
            "bank_authorisation_seconds": 35, "payment_capture_seconds": 37,
            "order_confirmation_seconds": 38, "escalation_seconds": 45,
            "session_ttl_seconds": 1800, 'supabase_timeout_seconds': 8}.items()}
        ints = {name: int(os.getenv(name.upper(), default)) for name, default in {
            "max_sessions": 200, "max_chat_messages": 30,
            "max_gemini_concurrency": 4, "session_creates_per_minute": 12}.items()}
        return cls(gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
                   gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
                   allow_chat_fallback=flag("ALLOW_CHAT_FALLBACK", True),
                   cors_origins=tuple(x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()),
                   frontend_dir=os.getenv("FRONTEND_DIR", ""),
                   storage_backend=os.getenv('STORAGE_BACKEND', 'auto').lower(),
                   supabase_url=os.getenv('SUPABASE_URL', '').strip().rstrip('/'),
                   supabase_secret_key=os.getenv('SUPABASE_SECRET_KEY', '') or os.getenv('SUPABASE_SERVICE_ROLE_KEY', ''),
                   require_shared_storage=os.getenv('VERCEL') == '1', **floats, **ints)
