"""Configuration management — persists token, deviceId, and coordinates to ~/.rappi/config.json.

The config directory defaults to ``~/.rappi`` and can be overridden with the
``RAPPI_CONFIG_DIR`` env var (e.g. a mounted Railway volume at ``/data/rappi``).
"""

import hashlib
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from rappi.constants import DEFAULT_LAT, DEFAULT_LNG

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".rappi"
CONFIG_FILE = CONFIG_DIR / "config.json"


def resolve_config_dir() -> Path:
    """Config directory: ``$RAPPI_CONFIG_DIR`` if set, else ``~/.rappi``. Resolved at call time."""
    override = os.environ.get("RAPPI_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".rappi"


def seed_fingerprint(seed: str) -> str:
    """Short, non-reversible fingerprint of an env token seed (never the token itself)."""
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


class RecentOrder(BaseModel):
    store_id: int
    store_name: str
    product_names: list[str] = []
    timestamp: str = ""  # ISO format


class RappiConfig(BaseModel):
    token: str | None = None
    refresh_token: str | None = None
    token_expires_at: str | None = None  # ISO-8601 UTC
    seed_fingerprint: str | None = None  # which env seed the persisted tokens descend from
    device_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    country: str = "co"  # co, mx, br, ar, cl, pe, ec, cr, uy
    lat: float = DEFAULT_LAT
    lng: float = DEFAULT_LNG
    active_address_id: int | None = None
    recent_orders: list[RecentOrder] = []
    favorite_store_ids: list[int] = []


class ConfigManager:
    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else resolve_config_dir() / "config.json"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config_dir(self) -> Path:
        return self._path.parent

    def _read_file(self) -> RappiConfig:
        if self._path.exists():
            return RappiConfig(**json.loads(self._path.read_text()))
        return RappiConfig()

    def _apply_env_seed(self, config: RappiConfig) -> RappiConfig:
        """Reconcile env token seeds with the persisted file.

        - No env seed: file only.
        - File holds tokens refreshed from the *same* env seed: the file wins.
        - Otherwise (new seed pushed, or legacy file without fingerprint): the env
          wins and is persisted with the new fingerprint.
        """
        env_token = os.environ.get("RAPPI_TOKEN") or None
        env_refresh = os.environ.get("RAPPI_REFRESH_TOKEN") or None
        seed = env_refresh or env_token
        if not seed:
            return config

        fingerprint = seed_fingerprint(seed)
        if config.token and config.seed_fingerprint == fingerprint:
            return config

        config.token = env_token
        config.refresh_token = env_refresh
        config.token_expires_at = None
        config.seed_fingerprint = fingerprint
        try:
            self.save(config)
        except OSError as e:
            # Read-only filesystem etc. — env tokens still work for this process.
            logger.warning("Could not persist env token seed to %s: %s", self._path, type(e).__name__)
        return config

    def load(self) -> RappiConfig:
        config = self._apply_env_seed(self._read_file())
        # Set RAPPI_COUNTRY from config so constants.py picks it up
        if config.country and not os.environ.get("RAPPI_COUNTRY"):
            os.environ["RAPPI_COUNTRY"] = config.country
        # Environment variables override file config (for remote deployment)
        env_device_id = os.environ.get("RAPPI_DEVICE_ID")
        if env_device_id:
            config.device_id = env_device_id
        env_lat = os.environ.get("RAPPI_LAT")
        env_lng = os.environ.get("RAPPI_LNG")
        if env_lat:
            config.lat = float(env_lat)
        if env_lng:
            config.lng = float(env_lng)
        return config

    def save(self, config: RappiConfig) -> None:
        """Atomically write the config (temp file in the same dir + os.replace), mode 0600."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(config.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        os.chmod(self._path, 0o600)

    def update(self, **kwargs) -> RappiConfig:
        """Load, update fields, save, and return the updated config."""
        config = self.load()
        for key, value in kwargs.items():
            setattr(config, key, value)
        self.save(config)
        return config
