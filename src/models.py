import os
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Section = Literal["01", "02"]
Modality = Literal["individual", "pareja", "trio"]
TEAM_SIZES: dict[Modality, int] = {"individual": 1, "pareja": 2, "trio": 3}


def validate_team_size(modality: Modality, count: int):
    if count != TEAM_SIZES[modality]:
        raise ValueError(f"La modalidad {modality} requiere {TEAM_SIZES[modality]} integrante(s).")
CARNET_PATTERN = r"^[0-9]{7}$"
GITHUB_USER_PATTERN = r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$"


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_user: Annotated[str, Field(max_length=39, pattern=GITHUB_USER_PATTERN)]
    carnet: Annotated[str, Field(max_length=64)]

    @field_validator("carnet")
    @classmethod
    def validate_carnet(cls, value: str) -> str:
        if not re.fullmatch(os.getenv("CARNET_PATTERN", CARNET_PATTERN), value):
            raise ValueError("El carné no cumple el formato del curso. Revisa los caracteres y ceros iniciales.")
        return value
