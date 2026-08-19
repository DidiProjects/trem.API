from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional


class ProfileName(StrEnum):
    """Perfis de acesso reconhecidos pela API."""

    FILE_EDITOR = "file_editor"
    AIRLINE_COMPANY = "airline_company"


@dataclass
class Profile:
    id: str
    name: str
    description: Optional[str]
    created_at: datetime


# Constantes retrocompatíveis — preferir ProfileName nos códigos novos
FILE_EDITOR = ProfileName.FILE_EDITOR
AIRLINE_COMPANY = ProfileName.AIRLINE_COMPANY
