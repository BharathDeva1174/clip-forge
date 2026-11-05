"""The integration endpoints.

These are the endpoints behind the Integrations page: saving a provider
configuration, keeping its key, looking up its models, testing it, and
choosing which one the highlight detection uses.

No response from this router contains an API key, and no request is sent to
a provider unless the user asked for it.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.ai_client import AiClient, AiClientError, validate_base_url
from app.credentials import (
    CredentialStore,
    CredentialStoreError,
    new_reference,
    resolve_secret,
)
from app.db import (
    create_integration,
    delete_integration,
    get_integration,
    list_integrations,
    record_integration_test,
    set_default_integration,
    update_integration,
)
from app.deps import get_ai_client, get_credential_store, get_db
from app.errors import ApiError, ErrorCode
from app.integrations import (
    ApiProtocol,
    ConnectionStatus,
    IntegrationRecord,
    IntegrationRole,
    ModelCapabilities,
    PricingStatus,
    StructuredOutputMode,
)
from app.providers import PROVIDER_PRESETS, get_preset
from app.schemas import (
    ApiKeyRequest,
    ConnectionTestResponse,
    IntegrationCreateRequest,
    IntegrationListResponse,
    IntegrationResponse,
    IntegrationUpdateRequest,
    ModelDescriptor,
    ModelListResponse,
    ProviderPresetListResponse,
    ProviderPresetResponse,
)

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

#: The largest page of configurations a client may ask for.
MAX_PAGE_SIZE = 100

#: The name of the note that accompanies a model list.
MODEL_LIST_NOTE = (
    "This is what the provider advertises. It does not prove that a model "
    "answers, and it does not say what a model costs: pricing and "
    "availability are the provider's to change."
)


def _provider_name(provider_id: str) -> str:
    """The display name of a provider."""
    preset = get_preset(provider_id)
    return preset.name if preset else provider_id


def _store_key(
    store: CredentialStore, record: IntegrationRecord, api_key: str
) -> str:
    """Save a key and return its reference.

    The reference is new every time a key is replaced, so the old entry in
    the credential store does not linger.
    """
    reference = new_reference()
    store.store(reference, api_key)
    if record.secret_ref:
        store.delete(record.secret_ref)
    return reference


@router.get(
    "/presets",
    response_model=ProviderPresetListResponse,
    summary="The provider presets",
    description=(
        "The providers a configuration can start from, with their default "
        "base URLs and protocols. The values are defaults: every one of "
        "them can be edited."
    ),
)
def list_presets() -> ProviderPresetListResponse:
    return ProviderPresetListResponse(
        items=[
            ProviderPresetResponse(
                id=preset.id,
                name=preset.name,
                summary=preset.summary,
                default_protocol=preset.default_protocol,
                base_urls={
                    protocol.value: url
                    for protocol, url in preset.base_urls.items()
                },
                models_url=preset.models_url,
                supports_model_discovery=preset.supports_model_discovery,
                free_tier=preset.free_tier,
                free_tier_note=preset.free_tier_note,
                docs_url=preset.docs_url,
            )
            for preset in PROVIDER_PRESETS
        ]
    )


@router.get(
    "",
    response_model=IntegrationListResponse,
    summary="List the saved configurations",
)
def list_saved(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> IntegrationListResponse:
    records = list_integrations(conn)
    page = records[offset : offset + limit]
    return IntegrationListResponse(
        items=[
            IntegrationResponse.from_record(
                record, provider_name=_provider_name(record.provider_id)
            )
            for record in page
        ],
        total=len(records),
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=IntegrationResponse,
    status_code=201,
    summary="Save a configuration",
    description=(
        "The base URL is checked before the configuration is saved, so an "
        "unusable endpoint is refused immediately. The API key, when there "
        "is one, goes to the operating system's credential store and is "
        "never returned."
    ),
)
def create_saved(
    body: IntegrationCreateRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> IntegrationResponse:
    preset = get_preset(body.provider_id)
    if preset is None:
        raise ApiError(
            400,
            ErrorCode.INTEGRATION_INVALID.value,
            "That provider is not one ClipForge knows about.",
        )

    protocol = body.protocol or preset.default_protocol
    base_url = body.base_url
    if not base_url:
        base_url = preset.base_url_for(protocol) or ""
    if not base_url:
        raise ApiError(
            400,
            ErrorCode.INTEGRATION_INVALID.value,
            "A base URL is required for this provider.",
        )
    try:
        base_url = validate_base_url(base_url, allow_local=body.local_endpoint)
    except AiClientError as exc:
        raise ApiError(400, exc.code, exc.message) from exc

    capabilities = ModelCapabilities(
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        structured_output=body.structured_output or StructuredOutputMode.NONE,
        metadata_source="user",
    )

    record = create_integration(
        conn,
        name=body.name,
        provider_id=body.provider_id,
        protocol=protocol,
        base_url=base_url,
        model_id=body.model_id,
        model_name=body.model_name,
        capabilities=capabilities,
        reasoning_effort=body.reasoning_effort,
        pricing_status=body.pricing_status,
        local_endpoint=body.local_endpoint,
        enabled=body.enabled,
        is_default=body.is_default,
    )

    if body.api_key:
        try:
            reference = _store_key(store, record, body.api_key)
        except CredentialStoreError as exc:
            raise ApiError(400, exc.code, exc.message) from exc
        record = update_integration(conn, record.id, secret_ref=reference) or record

    return IntegrationResponse.from_record(
        record, provider_name=_provider_name(record.provider_id)
    )


@router.get(
    "/{integration_id}",
    response_model=IntegrationResponse,
    summary="Read one configuration",
    responses={404: {"description": "No configuration with that id exists."}},
)
def read_one(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> IntegrationResponse:
    record = _require(conn, integration_id)
    return IntegrationResponse.from_record(
        record, provider_name=_provider_name(record.provider_id)
    )


@router.patch(
    "/{integration_id}",
    response_model=IntegrationResponse,
    summary="Edit a configuration",
    responses={404: {"description": "No configuration with that id exists."}},
)
def edit_one(
    integration_id: str,
    body: IntegrationUpdateRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> IntegrationResponse:
    record = _require(conn, integration_id)

    base_url = record.base_url
    if body.base_url is not None:
        base_url = body.base_url
    local_endpoint = (
        body.local_endpoint
        if body.local_endpoint is not None
        else record.local_endpoint
    )
    try:
        base_url = validate_base_url(base_url, allow_local=local_endpoint)
    except AiClientError as exc:
        raise ApiError(400, exc.code, exc.message) from exc

    capabilities = ModelCapabilities(
        context_window=(
            body.context_window
            if body.context_window is not None
            else record.capabilities.context_window
        ),
        max_output_tokens=(
            body.max_output_tokens
            if body.max_output_tokens is not None
            else record.capabilities.max_output_tokens
        ),
        reasoning_effort_parameter=record.capabilities.reasoning_effort_parameter,
        reasoning_effort_values=record.capabilities.reasoning_effort_values,
        structured_output=(
            body.structured_output
            if body.structured_output is not None
            else record.capabilities.structured_output
        ),
        available=record.capabilities.available,
        metadata_source=record.capabilities.metadata_source,
        verified_at=record.capabilities.verified_at,
    )

    updated = update_integration(
        conn,
        integration_id,
        name=body.name,
        protocol=body.protocol,
        base_url=base_url,
        model_id=body.model_id,
        model_name=body.model_name,
        capabilities=capabilities,
        reasoning_effort=body.reasoning_effort,
        pricing_status=body.pricing_status,
        local_endpoint=body.local_endpoint,
        enabled=body.enabled,
    )
    if updated is None:
        raise ApiError(
            404, ErrorCode.INTEGRATION_NOT_FOUND.value, "That configuration is gone."
        )
    return IntegrationResponse.from_record(
        updated, provider_name=_provider_name(updated.provider_id)
    )


@router.post(
    "/{integration_id}/key",
    response_model=IntegrationResponse,
    summary="Replace the API key",
    description=(
        "The key is written to the operating system's credential store. It "
        "is never returned and never stored in the database."
    ),
    responses={404: {"description": "No configuration with that id exists."}},
)
def replace_key(
    integration_id: str,
    body: ApiKeyRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> IntegrationResponse:
    record = _require(conn, integration_id)
    try:
        reference = _store_key(store, record, body.api_key)
    except CredentialStoreError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    updated = update_integration(conn, integration_id, secret_ref=reference) or record
    return IntegrationResponse.from_record(
        updated, provider_name=_provider_name(updated.provider_id)
    )


@router.delete(
    "/{integration_id}/key",
    response_model=IntegrationResponse,
    summary="Remove the API key",
    responses={404: {"description": "No configuration with that id exists."}},
)
def remove_key(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> IntegrationResponse:
    record = _require(conn, integration_id)
    if record.secret_ref:
        store.delete(record.secret_ref)
    updated = (
        update_integration(conn, integration_id, clear_secret=True) or record
    )
    return IntegrationResponse.from_record(
        updated, provider_name=_provider_name(updated.provider_id)
    )


@router.post(
    "/{integration_id}/select",
    response_model=IntegrationResponse,
    summary="Make this the default for highlight detection",
    responses={404: {"description": "No configuration with that id exists."}},
)
def select_default(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> IntegrationResponse:
    _require(conn, integration_id)
    updated = set_default_integration(
        conn, integration_id, IntegrationRole.HIGHLIGHT_DETECTION
    )
    if updated is None:
        raise ApiError(
            404, ErrorCode.INTEGRATION_NOT_FOUND.value, "That configuration is gone."
        )
    return IntegrationResponse.from_record(
        updated, provider_name=_provider_name(updated.provider_id)
    )


@router.delete(
    "/{integration_id}",
    status_code=204,
    # 204 carries no body. The explicit None stops older FastAPI versions
    # from inferring one from the ``-> None`` annotation under postponed
    # evaluation, which fails at import time.
    response_model=None,
    summary="Remove a configuration",
    responses={404: {"description": "No configuration with that id exists."}},
)
def remove_one(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> None:
    record = _require(conn, integration_id)
    if record.secret_ref:
        store.delete(record.secret_ref)
    delete_integration(conn, integration_id)


@router.get(
    "/{integration_id}/models",
    response_model=ModelListResponse,
    summary="Fetch the models this provider offers",
    description=(
        "Asks the provider for its model list. The identifiers are the "
        "provider's own, exactly. A provider that does not publish a list "
        "returns an error, and the model can still be typed in by hand."
    ),
    responses={404: {"description": "No configuration with that id exists."}},
)
def fetch_models(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    client: Annotated[AiClient, Depends(get_ai_client)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> ModelListResponse:
    record = _require(conn, integration_id)
    preset = get_preset(record.provider_id)
    models_url = preset.models_url if preset else None
    if not models_url:
        raise ApiError(
            400,
            ErrorCode.AI_NOT_SUPPORTED.value,
            "This provider does not publish a model list. Type the model id in "
            "by hand.",
        )

    api_key = resolve_secret(store, record.secret_ref)
    try:
        models = client.list_models(record, api_key, models_url=models_url)
    except AiClientError as exc:
        raise ApiError(400, exc.code, exc.message) from exc

    items = []
    for model in models:
        protocol = model.protocol
        if protocol is None and preset is not None:
            protocol = preset.protocol_for_model(model.id)
        items.append(
            ModelDescriptor(
                id=model.id,
                name=model.name,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                structured_output=model.structured_output,
                available=model.available,
                protocol=protocol,
            )
        )
    return ModelListResponse(
        items=items, source_url=models_url, note=MODEL_LIST_NOTE
    )


@router.post(
    "/{integration_id}/test",
    response_model=ConnectionTestResponse,
    summary="Test the connection",
    description=(
        "Sends one small request that asks the model for a single word. No "
        "transcript, no video and no user content is sent."
    ),
    responses={404: {"description": "No configuration with that id exists."}},
)
def test_connection(
    integration_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    client: Annotated[AiClient, Depends(get_ai_client)],
    store: Annotated[CredentialStore, Depends(get_credential_store)],
) -> ConnectionTestResponse:
    record = _require(conn, integration_id)
    api_key = resolve_secret(store, record.secret_ref)
    outcome = client.test_connection(record, api_key)
    record_integration_test(
        conn,
        integration_id,
        status=outcome.status,
        code=outcome.code,
    )
    return ConnectionTestResponse(
        status=outcome.status,
        code=outcome.code,
        message=outcome.message,
        model=outcome.model,
    )


def _require(conn: sqlite3.Connection, integration_id: str) -> IntegrationRecord:
    """The configuration, or a 404."""
    record = get_integration(conn, integration_id)
    if record is None:
        raise ApiError(
            404,
            ErrorCode.INTEGRATION_NOT_FOUND.value,
            "No configuration with that id exists.",
        )
    return record
