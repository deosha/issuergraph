"""External processing: Textract OCR and a Bedrock model, both optional.

Nothing here runs unless ASSESS_AWS_PROFILE names an AWS profile — then, and
only then, page images go to Textract and page text goes to Bedrock in that
profile's account and region. There is no silent fallback to any other
service: an unavailable provider raises `ProviderUnavailable`, and the
document is marked failed with that reason.

`FixtureProvider` replays recorded responses for the synthetic demo documents
(keyed by file hash). It makes no network call, and every fact it yields is
labelled as coming from a synthetic fixture.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field

from .. import settings

MAX_OUTPUT_TOKENS = 32000


class ProviderUnavailable(RuntimeError):
    """No configured, working provider. The message is safe to show."""


@dataclass
class Usage:
    model_id: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    ocr_pages: int = 0
    notes: list[str] = field(default_factory=list)


class ModelProvider:
    name = "none"
    synthetic = False

    def structured(self, *, system: str, user: str, tool_name: str, schema: dict,
                   usage: Usage, fixture_key: str | None = None) -> dict:
        raise NotImplementedError


class FixtureProvider(ModelProvider):
    """Recorded responses for synthetic fixtures; refuses anything else."""
    name = "synthetic-fixture"
    synthetic = True

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses

    def structured(self, *, system, user, tool_name, schema, usage, fixture_key=None):
        key = f"{fixture_key}:{tool_name}"
        if key not in self.responses:
            raise ProviderUnavailable("No recorded synthetic response for this document; the "
                                      "fixture provider never processes real documents.")
        usage.model_id = "synthetic-fixture (no model call)"
        return json.loads(json.dumps(self.responses[key]))


class BedrockProvider(ModelProvider):
    name = "bedrock"

    def __init__(self, session, model_id: str):
        self.client = session.client("bedrock-runtime")
        self.model_id = model_id

    def structured(self, *, system, user, tool_name, schema, usage, fixture_key=None):
        from botocore.exceptions import ClientError

        usage.model_id = self.model_id
        request = dict(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
            toolConfig={"tools": [{"toolSpec": {"name": tool_name,
                                                "description": "Record the extracted fields.",
                                                "inputSchema": {"json": schema}}}],
                        "toolChoice": {"tool": {"name": tool_name}}},
        )
        last = None
        for attempt in range(3):
            try:
                resp = self.client.converse(**request)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                last = code
                if code in ("ThrottlingException", "ServiceUnavailableException",
                            "ModelNotReadyException") and attempt < 2:
                    usage.retries += 1
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ProviderUnavailable(f"Bedrock refused the request ({code}).") from None
            usage.calls += 1
            u = resp.get("usage", {})
            usage.input_tokens += u.get("inputTokens", 0)
            usage.output_tokens += u.get("outputTokens", 0)
            for block in resp["output"]["message"]["content"]:
                if "toolUse" in block:
                    return block["toolUse"]["input"]
            if resp.get("stopReason") == "max_tokens":
                raise ProviderUnavailable("Model output was cut off (max tokens).")
            usage.retries += 1
        raise ProviderUnavailable(f"Model returned no structured output ({last or 'no tool call'}).")


def _session():
    profile = settings.assess_aws_profile()
    if not profile:
        raise ProviderUnavailable("External processing is off: ASSESS_AWS_PROFILE is not set.")
    try:
        import boto3

        session = boto3.Session(profile_name=profile, region_name=settings.assess_aws_region())
        session.client("sts").get_caller_identity()
        return session
    except Exception as exc:  # credentials expired, profile missing, no network
        raise ProviderUnavailable(
            f"AWS profile '{profile}' is not usable ({type(exc).__name__}). "
            f"Run: aws login --profile {profile}") from None


def _pick_model(session) -> str:
    configured = settings.assess_bedrock_model()
    if configured:
        return configured
    try:
        profiles = session.client("bedrock").list_inference_profiles()["inferenceProfileSummaries"]
    except Exception as exc:
        raise ProviderUnavailable(f"Cannot list Bedrock models ({type(exc).__name__}); set "
                                  "ASSESS_BEDROCK_MODEL.") from None
    ids = [p["inferenceProfileId"] for p in profiles
           if "anthropic.claude" in p["inferenceProfileId"] and p.get("status", "ACTIVE") == "ACTIVE"]
    for family in ("sonnet", "opus", "haiku"):
        hits = sorted(i for i in ids if family in i)
        if hits:
            return hits[-1]
    raise ProviderUnavailable("No Anthropic Claude inference profile is available in "
                              f"{settings.assess_aws_region()}; set ASSESS_BEDROCK_MODEL.")


_cached: dict = {}


def model_provider() -> ModelProvider:
    if "model" not in _cached:
        session = _session()
        _cached["model"] = BedrockProvider(session, _pick_model(session))
    return _cached["model"]


def ocr_function():
    """A Textract-backed (png, width, height) -> (text, word_map), or None."""
    if not settings.assess_textract_enabled():
        return None
    try:
        session = _session()
    except ProviderUnavailable:
        return None
    client = session.client("textract")

    def ocr(png: bytes, width: float, height: float):
        resp = client.detect_document_text(Document={"Bytes": png})
        blocks = {b["Id"]: b for b in resp["Blocks"]}
        parts, word_map, cursor = [], [], 0
        lines = [b for b in resp["Blocks"] if b["BlockType"] == "LINE"]
        for li, line in enumerate(lines):
            if li:
                parts.append("\n")
                cursor += 1
            ids = [i for rel in line.get("Relationships", []) if rel["Type"] == "CHILD"
                   for i in rel["Ids"]]
            for wi, wid in enumerate(ids):
                w = blocks[wid]
                if w["BlockType"] != "WORD":
                    continue
                if wi:
                    parts.append(" ")
                    cursor += 1
                bb = w["Geometry"]["BoundingBox"]
                start = cursor
                parts.append(w["Text"])
                cursor += len(w["Text"])
                word_map.append([start, cursor, bb["Left"] * width, bb["Top"] * height,
                                 (bb["Left"] + bb["Width"]) * width,
                                 (bb["Top"] + bb["Height"]) * height])
        return "".join(parts), word_map

    return ocr


def provider_status() -> dict:
    """What the UI shows about external processing. No secrets."""
    try:
        p = model_provider()
        return {"model": p.model_id, "available": True,
                "textract": settings.assess_textract_enabled(),
                "region": settings.assess_aws_region()}
    except ProviderUnavailable as exc:
        return {"model": None, "available": False, "reason": str(exc)}


def load_fixture_provider(directory: pathlib.Path) -> FixtureProvider | None:
    path = directory / "model_responses.json"
    if not path.exists():
        return None
    return FixtureProvider(json.loads(path.read_text()))
