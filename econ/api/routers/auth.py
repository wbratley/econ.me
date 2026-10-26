import logging
import os
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from econ.api.auth import (
    MAGIC_LINK_EXPIRE_MINUTES,
    SUPPORTED_PROVIDERS,
    create_exchange_code,
    create_magic_token,
    create_token,
    decode_exchange_code,
    decode_magic_token,
    oauth,
)
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import MagicExchangeRequest, MagicLinkRequest, TokenResponse, UserRead
from econengine.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
# Where the verify redirect hands the exchange code to the spectator client.
WEB_CLIENT_URL = os.environ.get("ECON_WEB_CLIENT_URL", "http://localhost:5173")


@router.get("/login/{provider}")
async def login(provider: str, request: Request):
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    redirect_uri = f"{BASE_URL}/auth/callback/{provider}"
    client = oauth.create_client(provider)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/callback/{provider}", response_model=TokenResponse)
async def callback(provider: str, request: Request, session: Session = Depends(get_session)):
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    client = oauth.create_client(provider)
    try:
        token = await client.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth error: {exc}") from exc

    if provider == "google":
        userinfo = token.get("userinfo") or await client.userinfo(token=token)
        email: str = userinfo["email"]
        name: str = userinfo.get("name", email)
        provider_id: str = userinfo["sub"]
    else:  # github
        resp = await client.get("user", token=token)
        resp.raise_for_status()
        gh = resp.json()
        provider_id = str(gh["id"])
        name = gh.get("name") or gh["login"]
        if gh.get("email"):
            email = gh["email"]
        else:
            emails_resp = await client.get("user/emails", token=token)
            emails_resp.raise_for_status()
            emails = emails_resp.json()
            primary = next((e for e in emails if e.get("primary")), emails[0])
            email = primary["email"]

    user = session.query(User).filter_by(provider=provider, provider_id=provider_id).first()
    if user is None:
        user = User(email=email, name=name, provider=provider, provider_id=provider_id)
        session.add(user)
    else:
        user.email = email
        user.name = name
    session.commit()
    session.refresh(user)

    return TokenResponse(access_token=create_token(user.id, user.email, user.is_admin))


# --- Magic-link login -------------------------------------------------------
# POST /auth/magic/request emails a signed, short-lived link; GET
# /auth/magic/verify redeems it and 303s to the web client with a one-shot
# exchange code; POST /auth/magic/exchange swaps the code for the access
# token. The code detour keeps the access token out of URLs (and browser
# history), matching the client's "tokens in sessionStorage, never in the
# URL" rule. With no SMTP configured the link is logged to the server
# console instead — in development the operator IS the mail server.


def _deliver_magic_link(email: str, url: str) -> None:
    host = os.environ.get("ECON_SMTP_HOST", "")
    if not host:
        logger.warning("No ECON_SMTP_HOST configured — magic sign-in link for %s: %s", email, url)
        return
    port = int(os.environ.get("ECON_SMTP_PORT", "587"))
    username = os.environ.get("ECON_SMTP_USER", "")
    password = os.environ.get("ECON_SMTP_PASSWORD", "")
    sender = os.environ.get("ECON_SMTP_FROM", username or "econ.me <no-reply@localhost>")
    message = EmailMessage()
    message["Subject"] = "Your econ.me sign-in link"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Sign in to the living world:\n\n{url}\n\n"
        f"The link expires in {MAGIC_LINK_EXPIRE_MINUTES} minutes."
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if os.environ.get("ECON_SMTP_STARTTLS", "1") != "0":
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    except OSError as exc:
        # Delivery failed but the operator can still rescue the login.
        logger.error("Magic-link email to %s failed (%s) — link: %s", email, exc, url)


@router.post("/magic/request")
def request_magic_link(body: MagicLinkRequest, request: Request,
                       session: Session = Depends(get_session)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Not an email address")
    # Identity is the email address: a magic sign-in lands on the same
    # account an OAuth login with that address would have created.
    user = session.query(User).filter_by(email=email).first()
    if user is None:
        user = User(email=email, name=email.split("@", 1)[0],
                    provider="magic", provider_id=email)
        session.add(user)
        session.commit()
        session.refresh(user)
    # BASE_URL wins when set; otherwise the URL the requester actually used
    # (right for a dev box serving the link back to its own operator).
    base = os.environ.get("BASE_URL") or str(request.base_url).rstrip("/")
    url = f"{base}/auth/magic/verify?token={create_magic_token(user.id)}"
    _deliver_magic_link(email, url)
    return {"detail": "If that address can sign in, a link is on its way."}


@router.get("/magic/verify")
def verify_magic_link(token: str, session: Session = Depends(get_session)):
    payload = decode_magic_token(token)
    user = session.get(User, payload["sub"]) if payload else None
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in link")
    code = create_exchange_code(user.id)
    return RedirectResponse(f"{WEB_CLIENT_URL}/?code={code}", status_code=303)


@router.post("/magic/exchange", response_model=TokenResponse)
def exchange_magic_code(body: MagicExchangeRequest,
                        session: Session = Depends(get_session)):
    payload = decode_exchange_code(body.code)
    user = session.get(User, payload["sub"]) if payload else None
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in code")
    return TokenResponse(access_token=create_token(user.id, user.email, user.is_admin))


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)):
    return current_user
