from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str = Security(_header)) -> None:
    if key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
