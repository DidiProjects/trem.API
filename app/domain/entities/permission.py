import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional

# The key "<application>:<permission>" is what tokens, principals and the admin API carry:
# a permission only means something inside its application, and "ai" in one app must never
# grant "ai" in another.
APPLICATION_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,49}$"
PERMISSION_NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,49}$"
PERMISSION_KEY_PATTERN = (
    APPLICATION_SLUG_PATTERN[:-1] + ":" + PERMISSION_NAME_PATTERN[1:]
)
_KEY = re.compile(PERMISSION_KEY_PATTERN)

TREM_APP = "trem"


class TremPermission(StrEnum):
    """Permissions of this API itself, the application `trem`."""

    FILE_EDITOR = "trem:file_editor"
    AIRLINE_COMPANY = "trem:airline_company"
    AI = "trem:ai"
    LINKEDIN_SCRAPER = "trem:linkedin_scraper"
    ADMIN = "trem:admin"


def split_permission_key(key: str) -> tuple[str, str]:
    if not _KEY.match(key):
        raise ValueError(f"Permissão inválida: '{key}'. Formato: <aplicação>:<permissão>")
    application, name = key.split(":", 1)
    return application, name


@dataclass
class Application:
    id: str
    slug: str
    name: str
    description: Optional[str]
    created_at: datetime


@dataclass
class Permission:
    id: str
    application: str
    name: str
    description: Optional[str]
    created_at: datetime

    @property
    def key(self) -> str:
        return f"{self.application}:{self.name}"
