"""Shared model references for generated endpoint definitions."""

from pydantic import BaseModel, Field, ValidationInfo, field_serializer, field_validator


class ModelReference(BaseModel):
    ref: str = Field(..., description="A reference to a schema in the API specification")
    model: type[BaseModel] | None = Field(
        None, description="The Pydantic model representing the schema"
    )
    no_content: bool = Field(False, exclude_if=lambda value: not value)

    @field_serializer("model", when_used="json")
    def serialize_model(self, value):
        return value.__name__ if value is not None else None

    @field_validator("model", mode="before")
    @classmethod
    def resolve_model(cls, value, info: ValidationInfo):
        if isinstance(value, str):
            models = (info.context or {}).get("models", {})
            if value not in models:
                raise ValueError(f"Unknown model name: {value!r}")
            return models[value]
        return value

#Added the HTTP response codes, normal use case will be to only use the ones that are relevant to the endpoint, but this allows for a complete list of all possible responses to be defined in one place.
class ResponseCollection(BaseModel):
    model_config = {"extra": "forbid"}

    continue_: ModelReference | None = Field(None, alias="100")
    switching_protocols: ModelReference | None = Field(None, alias="101")
    processing: ModelReference | None = Field(None, alias="102")
    early_hints: ModelReference | None = Field(None, alias="103")
    upload_resumption_supported: ModelReference | None = Field(None, alias="104")

    success: ModelReference | None = Field(None, alias="200")
    created: ModelReference | None = Field(None, alias="201")
    accepted: ModelReference | None = Field(None, alias="202")
    non_authoritative_information: ModelReference | None = Field(None, alias="203")
    no_content: ModelReference | None = Field(None, alias="204")
    reset_content: ModelReference | None = Field(None, alias="205")
    partial_content: ModelReference | None = Field(None, alias="206")
    multi_status: ModelReference | None = Field(None, alias="207")
    already_reported: ModelReference | None = Field(None, alias="208")
    im_used: ModelReference | None = Field(None, alias="226")

    multiple_choices: ModelReference | None = Field(None, alias="300")
    moved_permanently: ModelReference | None = Field(None, alias="301")
    found: ModelReference | None = Field(None, alias="302")
    see_other: ModelReference | None = Field(None, alias="303")
    not_modified: ModelReference | None = Field(None, alias="304")
    use_proxy: ModelReference | None = Field(None, alias="305")
    unused_306: ModelReference | None = Field(None, alias="306")
    temporary_redirect: ModelReference | None = Field(None, alias="307")
    permanent_redirect: ModelReference | None = Field(None, alias="308")

    bad_request: ModelReference | None = Field(None, alias="400")
    unauthorized: ModelReference | None = Field(None, alias="401")
    payment_required: ModelReference | None = Field(None, alias="402")
    forbidden: ModelReference | None = Field(None, alias="403")
    not_found: ModelReference | None = Field(None, alias="404")
    method_not_allowed: ModelReference | None = Field(None, alias="405")
    not_acceptable: ModelReference | None = Field(None, alias="406")
    proxy_authentication_required: ModelReference | None = Field(None, alias="407")
    request_timeout: ModelReference | None = Field(None, alias="408")
    conflict: ModelReference | None = Field(None, alias="409")
    gone: ModelReference | None = Field(None, alias="410")
    length_required: ModelReference | None = Field(None, alias="411")
    precondition_failed: ModelReference | None = Field(None, alias="412")
    request_entity_too_large: ModelReference | None = Field(None, alias="413")
    request_uri_too_long: ModelReference | None = Field(None, alias="414")
    unsupported_media_type: ModelReference | None = Field(None, alias="415")
    requested_range_not_satisfiable: ModelReference | None = Field(None, alias="416")
    expectation_failed: ModelReference | None = Field(None, alias="417")
    im_a_teapot: ModelReference | None = Field(None, alias="418")
    misdirected_request: ModelReference | None = Field(None, alias="421")
    unprocessable_entity: ModelReference | None = Field(None, alias="422")
    locked: ModelReference | None = Field(None, alias="423")
    failed_dependency: ModelReference | None = Field(None, alias="424")
    too_early: ModelReference | None = Field(None, alias="425")
    upgrade_required: ModelReference | None = Field(None, alias="426")
    precondition_required: ModelReference | None = Field(None, alias="428")
    rate_limited: ModelReference | None = Field(None, alias="429")
    request_header_fields_too_large: ModelReference | None = Field(None, alias="431")
    unavailable_for_legal_reasons: ModelReference | None = Field(None, alias="451")

    internal_server_error: ModelReference | None = Field(None, alias="500")
    not_implemented: ModelReference | None = Field(None, alias="501")
    bad_gateway: ModelReference | None = Field(None, alias="502")
    service_unavailable: ModelReference | None = Field(None, alias="503")
    gateway_timeout: ModelReference | None = Field(None, alias="504")
    http_version_not_supported: ModelReference | None = Field(None, alias="505")
    variant_also_negotiates: ModelReference | None = Field(None, alias="506")
    insufficient_storage: ModelReference | None = Field(None, alias="507")
    loop_detected: ModelReference | None = Field(None, alias="508")
    not_extended: ModelReference | None = Field(None, alias="510")
    network_authentication_required: ModelReference | None = Field(None, alias="511")
