import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from econ.api.auth import SUPPORTED_PROVIDERS, create_token, oauth
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import TokenResponse, UserRead
from econengine.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


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


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)):
    return current_user
