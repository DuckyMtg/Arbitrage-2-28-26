import os
from fastapi import Header, HTTPException, status


def require_api_key(x_api_key: str | None = Header(default=None)):
    api_key = os.getenv("API_KEY")  # read at request time

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_KEY env var not set on server",
        )

    if x_api_key != api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return x_api_key
