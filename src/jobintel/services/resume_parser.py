"""Two-phase resume ingestion into versioned, citable candidate evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, ValidationError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceType,
    FrozenDomainModel,
    NonEmptyStr,
    UtcDateTime,
)
from jobintel.providers.base import (
    LLMProvider,
    Message,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    Usage,
)

RESUME_PARSER_VERSION = "jobintel-resume-parser-v1-zh-cn"
RESUME_SUBMIT_TOOL = "submit_candidate_profile"
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_CHARS = 100_000

RESUME_SYSTEM_PROMPT = (
    "你负责把候选人简历提取为严格的结构化证据。只保留简历明确陈述的事实, 不推测、不夸大。"
    "summary 使用简体中文概括候选人的定位、年限和核心方向。每条 evidence 只表达一段可独立引用的"
    "教育、工作经历、项目、技能或证书事实, 保留公司/学校、角色、时间、量化成果和技术名词。"
    "skills 只列该条证据明确支持的技术或领域能力, 去除重复项。所有自然语言字段使用简体中文, "
    "Python、Agent、RAG、API 等技术名词可保留英文。最后只调用 submit_candidate_profile 一次, "
    "不要返回普通文本或其他工具调用。"
)


class ResumeEvidenceDraft(FrozenDomainModel):
    """One model-extracted resume fact before program-owned identity assignment."""

    evidence_type: EvidenceType
    title: NonEmptyStr
    content: NonEmptyStr
    skills: tuple[NonEmptyStr, ...] = ()


class ResumeProfileDraft(FrozenDomainModel):
    """Structured resume extraction submitted by the model."""

    summary: NonEmptyStr
    evidence: tuple[ResumeEvidenceDraft, ...] = Field(min_length=1, max_length=100)


class CandidateProfilePreview(FrozenDomainModel):
    """Human-reviewable import artifact that is not yet persisted as a profile."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    source_name: NonEmptyStr
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: NonEmptyStr
    evidence: tuple[ResumeEvidenceDraft, ...] = Field(min_length=1)
    parser_version: NonEmptyStr = RESUME_PARSER_VERSION
    created_at: UtcDateTime


class ResumeParserTelemetry(FrozenDomainModel):
    """Content-free telemetry for a bounded resume parse."""

    attempts: int = Field(ge=1)
    repairs: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    parser_version: NonEmptyStr = RESUME_PARSER_VERSION


class ResumePreviewResult(FrozenDomainModel):
    """Validated preview plus parser telemetry."""

    preview: CandidateProfilePreview
    telemetry: ResumeParserTelemetry


class ResumeParserError(RuntimeError):
    """Raised when resume extraction exhausts its repair budget."""


def read_resume_text(path: Path) -> tuple[str, str]:
    """Read PDF, Markdown, or plain text and return normalized text plus SHA-256."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"无法读取简历文件: {exc}") from exc
    if size > MAX_RESUME_BYTES:
        raise ValueError("简历文件不能超过 10 MiB")
    suffix = path.suffix.casefold()
    try:
        raw = path.read_bytes()
        if suffix == ".pdf":
            reader = PdfReader(path)
            text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
        elif suffix in {".txt", ".md", ".markdown"}:
            text = raw.decode("utf-8")
        else:
            raise ValueError("仅支持 PDF、TXT、Markdown 简历")
    except (OSError, PyPdfError, UnicodeError) as exc:
        raise ValueError(f"简历文本提取失败: {exc}") from exc
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if len(normalized) < 20:
        raise ValueError("简历未提取到足够文本; 扫描版 PDF 请先进行 OCR")
    if len(normalized) > MAX_RESUME_CHARS:
        raise ValueError("简历文本超过 100000 字符")
    return normalized, hashlib.sha256(raw).hexdigest()


def stable_evidence_id(
    candidate_id: str,
    profile_version: int,
    source_order: int,
    evidence: ResumeEvidenceDraft,
) -> str:
    """Build a deterministic evidence identity from accepted preview content."""
    payload = {
        "candidate_id": candidate_id,
        "profile_version": profile_version,
        "source_order": source_order,
        "evidence": evidence.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"ev_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def materialize_profile(preview: CandidateProfilePreview) -> CandidateProfile:
    """Assign program-owned evidence IDs to one explicitly confirmed preview."""
    evidence = tuple(
        CandidateEvidence(
            evidence_id=stable_evidence_id(
                preview.candidate_id, preview.profile_version, index, draft
            ),
            source_order=index,
            **draft.model_dump(),
        )
        for index, draft in enumerate(preview.evidence)
    )
    return CandidateProfile(
        candidate_id=preview.candidate_id,
        profile_version=preview.profile_version,
        summary=preview.summary,
        evidence=evidence,
        source_sha256=preview.source_sha256,
        created_at=preview.created_at,
    )


class ResumeParserService:
    """Drive a provider through bounded, tool-submitted resume extraction."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_repairs: int = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure the provider, repair budget, and preview clock."""
        if max_repairs < 0:
            raise ValueError("max_repairs must be non-negative")
        self._provider = provider
        self._max_repairs = max_repairs
        self._clock = clock or (lambda: datetime.now(UTC))

    async def preview(
        self,
        *,
        candidate_id: str,
        profile_version: int,
        resume_file: Path,
    ) -> ResumePreviewResult:
        """Extract a reviewable preview without writing candidate tables."""
        text, source_sha256 = read_resume_text(resume_file)
        messages = [Message.user_text("请解析以下候选人简历:\n\n" + text)]
        usage = Usage()
        spec = self._tool_spec()
        for attempt in range(1, self._max_repairs + 2):
            turn = await self._provider.run_turn(RESUME_SYSTEM_PROMPT, messages, [spec])
            usage = usage + turn.usage
            messages.append(turn.assistant_message())
            submission, errors = self._submission(turn.tool_calls)
            if submission is not None:
                preview = CandidateProfilePreview(
                    candidate_id=candidate_id,
                    profile_version=profile_version,
                    source_name=resume_file.name,
                    source_sha256=source_sha256,
                    summary=submission.summary,
                    evidence=submission.evidence,
                    created_at=self._clock(),
                )
                return ResumePreviewResult(
                    preview=preview,
                    telemetry=ResumeParserTelemetry(
                        attempts=attempt,
                        repairs=attempt - 1,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    ),
                )
            if attempt <= self._max_repairs:
                messages.append(
                    Message(role="user", blocks=errors)
                    if errors
                    else Message.user_text("请仅调用 submit_candidate_profile 一次并匹配 Schema。")
                )
        raise ResumeParserError("简历解析在有限修复次数内未产生合法结构")

    @staticmethod
    def _tool_spec() -> ToolSpec:
        schema = ResumeProfileDraft.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(
            name=RESUME_SUBMIT_TOOL,
            description="提交从简历原文中提取的候选人摘要和可引用证据。",
            input_schema=schema,
        )

    @staticmethod
    def _submission(
        calls: Sequence[ToolCall],
    ) -> tuple[ResumeProfileDraft | None, list[ToolResultBlock]]:
        if len(calls) == 1 and calls[0].name == RESUME_SUBMIT_TOOL:
            call = calls[0]
            try:
                return ResumeProfileDraft.model_validate(call.arguments), []
            except ValidationError as exc:
                first = exc.errors(include_input=False)[0]
                field_path = ".".join(str(item) for item in first["loc"])
                content = json.dumps(
                    {
                        "code": "INVALID_RESUME_PROFILE",
                        "message": "候选人档案不符合 Schema",
                        "retryable": True,
                        "field_path": field_path,
                    },
                    ensure_ascii=False,
                )
                return None, [ToolResultBlock(tool_call_id=call.id, content=content, is_error=True)]
        return None, [
            ToolResultBlock(
                tool_call_id=call.id,
                content='{"code":"INVALID_RESUME_TURN","retryable":true}',
                is_error=True,
            )
            for call in calls
        ]
