import hmac
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import settings

_bearer_scheme = HTTPBearer(auto_error=False)


def verify_bearer(credentials: HTTPAuthorizationCredentials = Security(_bearer_scheme)) -> None:
    """Dependency: validates Bearer token using constant-time comparison.

    Raises HTTP 401 if the token is missing or incorrect.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_bytes = credentials.credentials.encode()
    expected_bytes = settings.bridge_token.encode()
    if not hmac.compare_digest(token_bytes, expected_bytes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
