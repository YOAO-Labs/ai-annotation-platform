"""Public invitation proof and authenticated acceptance HTTP presentation."""

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.v1.invitation_commands import (
    execute_invitation,
    get_authenticated_invitation_commands,
    get_invitation_commands,
)
from app.deps import bearer_scheme
from app.schemas.invitation import (
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    InvitationResolve,
    RegisterRequest,
    RegisterResponse,
)
from app.services.identity_invitation_commands import InvitationCommandsPort

router = APIRouter()


@router.get("/invitations/{token}", response_model=InvitationResolve)
async def resolve_invitation(
    token: str,
    request: Request,
    commands: InvitationCommandsPort = Depends(get_invitation_commands),
):
    return await execute_invitation(
        request, None, commands, "resolve_invitation", {"invitation_token": token}
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_via_invitation(
    payload: RegisterRequest,
    request: Request,
    commands: InvitationCommandsPort = Depends(get_invitation_commands),
):
    return await execute_invitation(
        request,
        None,
        commands,
        "register_invitation",
        {
            "invitation_token": payload.token,
            "name": payload.name,
            "password": payload.password,
        },
    )


@router.post("/invitations/accept", response_model=AcceptInvitationResponse)
async def accept_invitation_for_existing_user(
    payload: AcceptInvitationRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_authenticated_invitation_commands),
):
    """Explicitly attach the matching account without changing its platform role."""
    return await execute_invitation(
        request,
        credentials,
        commands,
        "accept_invitation",
        {"invitation_token": payload.token},
    )
