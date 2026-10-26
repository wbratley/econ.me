import os
from datetime import datetime, timedelta, timezone

from authlib.integrations.starlette_client import OAuth
from jose import JWTError, jwt

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"
# Deployment config: the default serves humans on the public site (an hour
# is a reasonable session); a harness that boots its own world for a
# multi-hour dynasty run sets this much higher in the environment.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

oauth = OAuth()

oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

oauth.register(
    name="github",
    client_id=os.environ.get("GITHUB_CLIENT_ID", ""),
    client_secret=os.environ.get("GITHUB_CLIENT_SECRET", ""),
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "read:user user:email"},
)

SUPPORTED_PROVIDERS = {"google", "github"}


def create_token(user_id: str, email: str, is_admin: bool) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "email": email, "is_admin": is_admin, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# --- Magic-link login -------------------------------------------------------
# Sign-in by email rides two purpose-scoped tokens, both signed with the same
# SECRET_KEY: the emailed magic link (minutes-scale TTL) and the one-shot
# exchange code the verify redirect hands to the web client (so the access
# token itself never appears in a URL). The `purpose` claim is what keeps
# these from moonlighting as bearer tokens — decode_access_token rejects them.

MAGIC_LINK_EXPIRE_MINUTES = int(os.environ.get("ECON_MAGIC_LINK_EXPIRE_MINUTES", "15"))
EXCHANGE_CODE_EXPIRE_MINUTES = 2


def _purpose_token(purpose: str, user_id: str, minutes: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload = {"sub": user_id, "purpose": purpose, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_purpose_token(token: str, purpose: str) -> dict | None:
    payload = decode_token(token)
    if payload is None or payload.get("purpose") != purpose:
        return None
    return payload


def create_magic_token(user_id: str) -> str:
    return _purpose_token("magic", user_id, MAGIC_LINK_EXPIRE_MINUTES)


def decode_magic_token(token: str) -> dict | None:
    return _decode_purpose_token(token, "magic")


def create_exchange_code(user_id: str) -> str:
    return _purpose_token("magic_exchange", user_id, EXCHANGE_CODE_EXPIRE_MINUTES)


def decode_exchange_code(code: str) -> dict | None:
    return _decode_purpose_token(code, "magic_exchange")


def decode_access_token(token: str) -> dict | None:
    """Bearer-token check: a plain access token, never a magic/exchange one."""
    payload = decode_token(token)
    if payload is None or payload.get("purpose") not in (None, "access"):
        return None
    return payload
