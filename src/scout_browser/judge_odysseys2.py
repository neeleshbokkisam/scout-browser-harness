"""Minimal Odysseys2 findings judge + deterministic scoring (EVALUATION.md)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from harness.llm.judge import Judge, JudgeOutputError, JudgeResponse
from harness.llm.types import Usage
from scout_browser.judge_images import (
    encode_image_parts,
    resolve_max_images,
    resolve_multimodal,
    select_screenshot_images,
)
from scout_browser.odysseys2 import Odysseys2Task
from scout_browser.paths import ODYSSEYS2
from scout_browser.trajectory import clip_middle, format_agent_trajectory

JUDGE_PROMPT_PATH = ODYSSEYS2 / "judge-prompt.txt"
FindingStatus = Literal["met", "violated", "not_assessable"]
logger = logging.getLogger(__name__)

# Cap judge packet size so Charon can still return structured JSON.
# OLD (pre-clipfix): answer=12_000 / tool=2_500 / user=100_000 / tight 6k·1.2k·60k
#   but clip_middle kept only head+tail=1600+1600 when over budget → ~3.2k of long answers.
# NEW: answer budget is actually retained (proportional head/tail); files protected & earlier;
#   trajectory is sacrificial on overflow (never clip_middle the whole packet).
MAX_ANSWER_CHARS = 24_000
MAX_TRAJECTORY_TOOL_CHARS = 2_000
MAX_OUTPUT_FILES_CHARS = 48_000
MAX_JUDGE_USER_CHARS = 120_000
TIGHT_ANSWER_CHARS = 12_000
TIGHT_TRAJECTORY_TOOL_CHARS = 1_000
TIGHT_OUTPUT_FILES_CHARS = 28_000
TIGHT_JUDGE_USER_CHARS = 80_000
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)

CLIP_BUDGET_DOC = {
    "old": {
        "max_answer_chars": 12_000,
        "max_tool_chars": 2_500,
        "max_output_files_chars": None,
        "max_user_chars": 100_000,
        "tight_answer_chars": 6_000,
        "tight_tool_chars": 1_200,
        "tight_user_chars": 60_000,
        "clip_middle_effective_answer": "1600+1600 when over max (bug)",
        "overflow": "shrink trajectory then clip_middle(entire prompt)",
        "packet_order": "task, website, rubric, trajectory, answer, output_files, screenshots",
    },
    "new": {
        "max_answer_chars": MAX_ANSWER_CHARS,
        "max_tool_chars": MAX_TRAJECTORY_TOOL_CHARS,
        "max_output_files_chars": MAX_OUTPUT_FILES_CHARS,
        "max_user_chars": MAX_JUDGE_USER_CHARS,
        "tight_answer_chars": TIGHT_ANSWER_CHARS,
        "tight_tool_chars": TIGHT_TRAJECTORY_TOOL_CHARS,
        "tight_output_files_chars": TIGHT_OUTPUT_FILES_CHARS,
        "tight_user_chars": TIGHT_JUDGE_USER_CHARS,
        "clip_middle_effective_answer": "≈max_answer_chars via proportional head/tail",
        "overflow": "shrink trajectory → answer → screenshots; files last; never whole-prompt clip",
        "packet_order": "task, website, rubric, output_files, answer, trajectory, screenshots",
    },
}


class Finding(BaseModel):
    item_id: str = Field(description="Rubric item id, e.g. R1")
    evidence: str = Field(description="Evidence explanation first, then status rationale")
    status: FindingStatus


class Odysseys2Judgment(BaseModel):
    agent_task_reading: str
    findings: list[Finding]
    observations: list[str] = Field(default_factory=list)
    infra_error: bool = False
    pii_present: bool = False
    reward_hacking_suspected: bool = False
    flag_notes: str | None = None


def load_judge_system_prompt(path=JUDGE_PROMPT_PATH) -> str:
    return path.read_text()


def format_rubric(task: Odysseys2Task) -> str:
    lines: list[str] = []
    for item in task.rubric:
        item_id = item.get("id") or item.get("item_id") or "?"
        name = item.get("name") or ""
        requirement = item.get("requirement") or item.get("description") or ""
        weight = item.get("weight", 1)
        lines.append(f"- {item_id} (weight={weight}) {name}\n  {requirement}")
    return "\n".join(lines) if lines else "(empty rubric)"


def format_output_files_section(
    output_files: list[dict[str, Any]] | None,
    *,
    max_chars: int = MAX_OUTPUT_FILES_CHARS,
) -> str:
    """Format deliverable previews; keep as much file text as the section budget allows."""
    if not output_files:
        return "(none — no delivered files)"
    # Prefer richer per-file budget when few files; still honor section cap.
    n = max(1, len(output_files))
    per_file = max(2_000, min(12_000, max_chars // n))
    blocks: list[str] = []
    used = 0
    for item in output_files:
        path = item.get("path") or item.get("name") or "?"
        preview = item.get("preview") or item.get("content")
        if preview is None or preview == "":
            preview = "(no preview — rejudge should reload from disk via collect_output_files)"
        remaining = max_chars - used
        if remaining <= 200:
            blocks.append(f"### {path}\n…[omitted: output_files section budget exhausted]…")
            break
        budget = min(per_file, remaining)
        clipped = clip_middle(str(preview), max_chars=budget)
        block = f"### {path}\n{clipped}"
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def format_screenshots_section(
    screenshots: list[dict[str, Any]] | None,
    *,
    attached_paths: list[str] | None = None,
) -> str:
    """Screenshot labels; optionally note which image bytes are attached."""
    if not screenshots:
        return "(none — no harness screenshots captured)"
    attached = {str(p) for p in (attached_paths or [])}
    lines: list[str] = []
    for i, shot in enumerate(screenshots, start=1):
        label = shot.get("label") or f"screenshot {i}"
        path = shot.get("screenshot") or shot.get("path") or "?"
        url = shot.get("url") or ""
        tool = shot.get("tool") or ""
        extra = []
        if tool:
            extra.append(f"after {tool}")
        if url:
            extra.append(f"url={url}")
        path_s = str(path)
        # Match by basename too (absolute vs run_dir relative).
        attached_here = path_s in attached or any(
            Path(path_s).name == Path(a).name for a in attached
        )
        if attached and attached_here:
            extra.append("IMAGE ATTACHED")
        elif attached:
            extra.append("label only")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"- [{label}] {path}{suffix}")
    if attached:
        lines.append(
            f"\n(Note: {len(attached)} screenshot image(s) are attached below the text packet. "
            "Trust attached images over agent claims when they disagree. "
            "Labels without IMAGE ATTACHED were not sent as pixels.)"
        )
    else:
        lines.append(
            "\n(Note: screenshot image bytes are not attached to this text judge packet; "
            "labels/paths above identify harness captures tied to browser actions. "
            "Prefer trajectory tool_result page text when images are unavailable.)"
        )
    return "\n".join(lines)


def _assemble_judge_packet(
    *,
    task: Odysseys2Task,
    files_section: str,
    clipped_answer: str,
    trajectory: str,
    shots_section: str,
    note: str = "",
) -> str:
    """Assemble judge user packet with files before answer (protected evidence first)."""
    body = (
        f"<task>\n{task.instruction}\n</task>\n\n"
        f"<website>\n{task.start_url}\n</website>\n\n"
        f"<rubric>\n{format_rubric(task)}\n</rubric>\n\n"
        f"<output_files>\n{files_section}\n</output_files>\n\n"
        f"<final_result>\n{clipped_answer}\n</final_result>\n\n"
        f"<agent_trajectory>\n{trajectory}\n</agent_trajectory>\n\n"
        f"<screenshots>\n{shots_section}\n</screenshots>\n"
    )
    if note:
        body += f"\n{note}\n"
    return body


def build_judge_user_prompt(
    task: Odysseys2Task,
    *,
    answer: str,
    messages: list[dict[str, Any]] | None,
    tool_history: list[dict[str, Any]] | None,
    screenshots: list[dict[str, Any]] | None = None,
    output_files: list[dict[str, Any]] | None = None,
    attached_paths: list[str] | None = None,
    max_answer_chars: int = MAX_ANSWER_CHARS,
    max_tool_chars: int = MAX_TRAJECTORY_TOOL_CHARS,
    max_output_files_chars: int = MAX_OUTPUT_FILES_CHARS,
    max_user_chars: int = MAX_JUDGE_USER_CHARS,
) -> str:
    """Build text judge packet.

    Priority on overflow (high → low): rubric/task, output_files, answer head/tail,
    screenshots labels, trajectory. Never ``clip_middle`` the whole packet (that used
    to drop mid-packet ``output_files``).
    """
    trajectory = format_agent_trajectory(
        messages=messages,
        tool_history=tool_history,
        max_tool_chars=max_tool_chars,
    )
    answer_budget = max_answer_chars
    files_budget = max_output_files_chars
    files_section = format_output_files_section(output_files, max_chars=files_budget)
    clipped_answer = clip_middle(answer or "(empty)", max_chars=answer_budget)
    shots_section = format_screenshots_section(screenshots, attached_paths=attached_paths)
    prompt = _assemble_judge_packet(
        task=task,
        files_section=files_section,
        clipped_answer=clipped_answer,
        trajectory=trajectory,
        shots_section=shots_section,
    )
    if len(prompt) <= max_user_chars:
        return prompt

    # 1) Shrink trajectory first (most expendable).
    overhead = len(prompt) - len(trajectory)
    traj_budget = max(1_500, max_user_chars - overhead - 200)
    trajectory = clip_middle(trajectory, max_chars=traj_budget)
    prompt = _assemble_judge_packet(
        task=task,
        files_section=files_section,
        clipped_answer=clipped_answer,
        trajectory=trajectory,
        shots_section=shots_section,
        note="(Note: trajectory clipped to fit judge context; omissions marked inline.)",
    )
    if len(prompt) <= max_user_chars:
        return prompt

    # 2) Shrink answer next (keep proportional head/tail).
    overhead = len(prompt) - len(clipped_answer)
    answer_budget = max(4_000, max_user_chars - overhead - 200)
    clipped_answer = clip_middle(answer or "(empty)", max_chars=answer_budget)
    prompt = _assemble_judge_packet(
        task=task,
        files_section=files_section,
        clipped_answer=clipped_answer,
        trajectory=trajectory,
        shots_section=shots_section,
        note=(
            "(Note: trajectory/answer clipped to fit judge context; "
            "output_files preserved preferentially; omissions marked inline.)"
        ),
    )
    if len(prompt) <= max_user_chars:
        return prompt

    # 3) Compact screenshot labels.
    shots_section = format_screenshots_section(screenshots, attached_paths=attached_paths)
    if len(shots_section) > 2_000:
        shots_section = clip_middle(shots_section, max_chars=2_000)
    prompt = _assemble_judge_packet(
        task=task,
        files_section=files_section,
        clipped_answer=clipped_answer,
        trajectory=trajectory,
        shots_section=shots_section,
        note=(
            "(Note: trajectory/answer/screenshots clipped; "
            "output_files + rubric preferred over mid-answer/trajectory.)"
        ),
    )
    if len(prompt) <= max_user_chars:
        return prompt

    # 4) Last resort: shrink files section (still head/tail per file), never whole-prompt clip.
    overhead = len(prompt) - len(files_section)
    files_budget = max(8_000, max_user_chars - overhead - 200)
    files_section = format_output_files_section(output_files, max_chars=files_budget)
    trajectory = clip_middle(trajectory, max_chars=min(1_500, len(trajectory)))
    prompt = _assemble_judge_packet(
        task=task,
        files_section=files_section,
        clipped_answer=clipped_answer,
        trajectory=trajectory,
        shots_section=shots_section,
        note=(
            "(Note: severe context pressure — files/answer head-tail retained; "
            "trajectory minimized.)"
        ),
    )
    return prompt


def extract_json_object(text: str) -> object:
    """Parse a JSON object from a judge reply; tolerate fences and surrounding prose."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty judge reply")

    candidates: list[str] = [raw]
    for match in _FENCE_RE.finditer(raw):
        candidates.append(match.group(1).strip())

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw[start : end + 1])

    last_error: Exception | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"no JSON object in judge reply: {last_error}")


def _schema_for_judge(judge: Judge, user: str) -> tuple[str, dict[str, Any] | None]:
    json_schema = Odysseys2Judgment.model_json_schema()
    if judge.client.supports_response_schema:
        from harness.llm.judge import strict_schema

        return user, {
            "name": Odysseys2Judgment.__name__,
            "schema": strict_schema(json_schema),
        }
    instruction = (
        f"{user}\n\nRespond with only a JSON object matching this JSON schema; it "
        f"replaces any other reply format requested above:\n{json.dumps(json_schema)}"
    )
    return instruction, None


async def _complete_text(
    judge: Judge,
    *,
    user: str,
    system: str,
    attempts: int = 3,
) -> tuple[Odysseys2Judgment, JudgeResponse]:
    instruction, response_schema = _schema_for_judge(judge, user)
    usage = Usage()
    cost: float | None = 0.0
    last_error: Exception | None = None
    last_text = ""
    for _ in range(attempts):
        response = await judge.complete(
            instruction, system=system, response_schema=response_schema
        )
        usage = usage + response.usage
        cost = None if cost is None or response.cost_usd is None else cost + response.cost_usd
        last_text = response.text or ""
        try:
            parsed = Odysseys2Judgment.model_validate(extract_json_object(last_text))
        except (ValueError, ValidationError) as error:
            last_error = error
            continue
        return parsed, JudgeResponse(last_text, usage, cost)
    err = JudgeOutputError(
        f"judge returned no valid Odysseys2Judgment: {last_error}; "
        f"raw_chars={len(last_text)}"
    )
    err.last_raw_text = last_text  # type: ignore[attr-defined]
    raise err


async def _complete_multimodal(
    judge: Judge,
    *,
    user: str,
    system: str,
    image_parts: list[dict[str, Any]],
    attempts: int = 3,
) -> tuple[Odysseys2Judgment, JudgeResponse]:
    """Call Responses API with text + input_image parts (bypasses Judge.complete str-only)."""
    instruction, response_schema = _schema_for_judge(judge, user)
    usage = Usage()
    cost: float | None = 0.0
    last_error: Exception | None = None
    last_text = ""
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": instruction}, *image_parts],
        }
    )
    for _ in range(attempts):
        generation = await judge.client.generate(
            messages,
            max_output_tokens=judge.max_output_tokens,
            reasoning_effort=judge.reasoning_effort,
            response_schema=response_schema,
        )
        usage = usage + generation.usage
        cost = (
            None
            if cost is None or generation.cost_usd is None
            else cost + generation.cost_usd
        )
        last_text = generation.text or ""
        try:
            parsed = Odysseys2Judgment.model_validate(extract_json_object(last_text))
        except (ValueError, ValidationError) as error:
            last_error = error
            continue
        return parsed, JudgeResponse(last_text, usage, cost)
    err = JudgeOutputError(
        f"multimodal judge returned no valid Odysseys2Judgment: {last_error}; "
        f"raw_chars={len(last_text)}"
    )
    err.last_raw_text = last_text  # type: ignore[attr-defined]
    raise err


async def _complete_judgment(
    judge: Judge,
    *,
    user: str,
    system: str,
    attempts: int = 3,
    image_parts: list[dict[str, Any]] | None = None,
) -> tuple[Odysseys2Judgment, JudgeResponse]:
    """Judge JSON completion; optional screenshot image parts."""
    if image_parts:
        return await _complete_multimodal(
            judge,
            user=user,
            system=system,
            image_parts=image_parts,
            attempts=attempts,
        )
    return await _complete_text(judge, user=user, system=system, attempts=attempts)


def score_judgment(task: Odysseys2Task, judgment: Odysseys2Judgment) -> dict[str, Any]:
    """Deterministic score policy from EVALUATION.md (0–100 item credit).

    Also computes assessable-only credit: not_assessable excluded from both
    earned and denominator (same grade.json, no extra judge run).
    """
    weights: dict[str, float] = {}
    for item in task.rubric:
        item_id = str(item.get("id") or item.get("item_id") or "")
        weights[item_id] = float(item.get("weight", 1) or 1)

    by_id = {f.item_id: f for f in judgment.findings}
    earned = 0.0
    total = 0.0
    assessable_earned = 0.0
    assessable_total = 0.0
    per_item: list[dict[str, Any]] = []
    for item_id, weight in weights.items():
        total += weight
        finding = by_id.get(item_id)
        status = finding.status if finding else "not_assessable"
        credit = weight if status == "met" else 0.0
        earned += credit
        if status != "not_assessable":
            assessable_total += weight
            assessable_earned += credit
        per_item.append(
            {
                "item_id": item_id,
                "weight": weight,
                "status": status,
                "credit": credit,
                "missing_finding": finding is None,
            }
        )

    item_credit = 0.0 if total <= 0 else 100.0 * earned / total
    assessable_only = (
        None if assessable_total <= 0 else 100.0 * assessable_earned / assessable_total
    )
    rh_zeroed = bool(judgment.reward_hacking_suspected)
    final_score = 0.0 if rh_zeroed else item_credit
    assessable_score = None if assessable_only is None else (0.0 if rh_zeroed else assessable_only)
    return {
        "item_credit": item_credit,
        "score": final_score,
        "assessable_only": assessable_only,
        "assessable_score": assessable_score,
        "earned_weight": earned,
        "total_weight": total,
        "assessable_earned_weight": assessable_earned,
        "assessable_total_weight": assessable_total,
        "rh_zeroed": rh_zeroed,
        "per_item": per_item,
    }


def _is_vision_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    needles = (
        "image",
        "vision",
        "multimodal",
        "input_image",
        "unsupported",
        "invalid_image",
        "could not process image",
        "media type",
        "content type",
    )
    return any(n in text for n in needles)


async def grade_result(
    task: Odysseys2Task,
    result: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str | None = "high",
    screenshots: list[dict[str, Any]] | None = None,
    output_files: list[dict[str, Any]] | None = None,
    multimodal: bool | None = None,
    max_images: int | None = None,
    run_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Call the findings judge and attach deterministic scores + judge cost.

    Retries once with tighter clipping if the first JSON parse fails.
    When multimodal is enabled, attaches up to N screenshot PNGs; on vision
    API failure falls back once to text-only.
    """
    shots = screenshots if screenshots is not None else result.get("screenshots")
    files = output_files if output_files is not None else result.get("output_files")
    if isinstance(shots, str):
        shots = None
    if isinstance(files, str):
        files = None
    shot_list = shots if isinstance(shots, list) else None
    file_list = files if isinstance(files, list) else None
    system = load_judge_system_prompt()
    answer = str(result.get("answer") or "")
    messages = result.get("messages")
    tool_history = result.get("tool_history")
    run_path = Path(run_dir) if run_dir else None

    use_mm = resolve_multimodal(multimodal)
    n_max = resolve_max_images(max_images)
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    image_parts: list[dict[str, Any]] = []
    attached_paths: list[str] = []
    vision_error: str | None = None
    fallback: str | None = None

    if use_mm and shot_list:
        selected, skipped = select_screenshot_images(
            shot_list,
            run_dir=run_path,
            max_images=n_max,
        )
        if selected:
            try:
                image_parts = encode_image_parts(selected)
                attached_paths = [s["path"] for s in selected]
            except OSError as exc:
                vision_error = f"encode_failed: {exc}"
                image_parts = []
                attached_paths = []
                fallback = "text_only"
                logger.warning("screenshot encode failed; text-only fallback: %s", exc)

    mm_meta: dict[str, Any] = {
        "enabled": use_mm,
        "images_sent": len(attached_paths),
        "image_paths": list(attached_paths),
        "image_labels": [s.get("label") for s in selected],
        "skipped": skipped,
        "vision_error": vision_error,
        "fallback": fallback,
        "max_images": n_max,
    }
    if attached_paths:
        logger.info(
            "multimodal judge attaching %d image(s): %s",
            len(attached_paths),
            [Path(p).name for p in attached_paths],
        )

    clip_passes = (
        {
            "max_answer_chars": MAX_ANSWER_CHARS,
            "max_tool_chars": MAX_TRAJECTORY_TOOL_CHARS,
            "max_output_files_chars": MAX_OUTPUT_FILES_CHARS,
            "max_user_chars": MAX_JUDGE_USER_CHARS,
        },
        {
            "max_answer_chars": TIGHT_ANSWER_CHARS,
            "max_tool_chars": TIGHT_TRAJECTORY_TOOL_CHARS,
            "max_output_files_chars": TIGHT_OUTPUT_FILES_CHARS,
            "max_user_chars": TIGHT_JUDGE_USER_CHARS,
        },
    )

    judge = Judge(
        model=model,
        reasoning_effort=reasoning_effort,
        max_output_tokens=32_000,
        json_attempts=3,
    )
    parsed: Odysseys2Judgment | None = None
    response: JudgeResponse | None = None
    last_error: Exception | None = None
    last_raw = ""
    total_cost: float | None = 0.0
    total_usage = Usage()
    active_parts: list[dict[str, Any]] | None = image_parts or None
    active_attached = list(attached_paths)

    async def _run_clip_passes(
        parts: list[dict[str, Any]] | None,
        paths: list[str],
    ) -> tuple[Odysseys2Judgment | None, JudgeResponse | None, Exception | None, str, float | None]:
        local_parsed: Odysseys2Judgment | None = None
        local_response: JudgeResponse | None = None
        local_error: Exception | None = None
        local_raw = ""
        local_cost: float | None = 0.0
        for clip_kwargs in clip_passes:
            user = build_judge_user_prompt(
                task,
                answer=answer,
                messages=messages,
                tool_history=tool_history,
                screenshots=shot_list,
                output_files=file_list,
                attached_paths=paths or None,
                **clip_kwargs,
            )
            try:
                local_parsed, local_response = await _complete_judgment(
                    judge,
                    user=user,
                    system=system,
                    attempts=3,
                    image_parts=parts,
                )
                if local_cost is not None and local_response.cost_usd is not None:
                    local_cost += local_response.cost_usd
                elif local_response.cost_usd is None:
                    local_cost = None
                return local_parsed, local_response, None, local_response.text or "", local_cost
            except JudgeOutputError as exc:
                local_error = exc
                local_raw = getattr(exc, "last_raw_text", None) or str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 — vision/API errors need fallback
                local_error = exc
                local_raw = str(exc)
                # Non-parse API failures should not burn the tight clip pass if vision broke.
                if parts and _is_vision_error(exc):
                    return None, None, exc, local_raw, local_cost
                continue
        return local_parsed, local_response, local_error, local_raw, local_cost

    parsed, response, last_error, last_raw, pass_cost = await _run_clip_passes(
        active_parts, active_attached
    )
    if pass_cost is None:
        total_cost = None
    elif total_cost is not None:
        total_cost += pass_cost
    if response is not None:
        total_usage = total_usage + response.usage

    # Multimodal path failed entirely → one text-only retry (keeps text path working).
    if (parsed is None or response is None) and active_parts:
        err_s = f"{type(last_error).__name__}: {last_error}" if last_error else "multimodal_failed"
        logger.warning("multimodal judge failed (%s); retrying text-only", err_s)
        mm_meta["vision_error"] = err_s
        mm_meta["fallback"] = "text_only"
        mm_meta["images_sent"] = 0
        parsed, response, last_error, last_raw, pass_cost = await _run_clip_passes(None, [])
        if pass_cost is None:
            total_cost = None
        elif total_cost is not None:
            total_cost += pass_cost
        if response is not None:
            total_usage = total_usage + response.usage

    n_shots = len(shot_list) if shot_list else 0
    n_files = len(file_list) if file_list else 0
    n_attached = int(mm_meta.get("images_sent") or 0)
    if n_shots == 0 and n_files == 0:
        bias = (
            "No screenshots/output_files in judge packet; "
            "items needing page/screenshot corroboration may be not_assessable or violated. "
            "Trajectory tool results (if any) remain the primary corroboration channel."
        )
    elif use_mm and n_attached > 0 and mm_meta.get("fallback") is None:
        bias = (
            f"Judge packet includes {n_shots} screenshot label(s), {n_attached} attached image(s), "
            f"and {n_files} output file(s)."
        )
    elif use_mm and mm_meta.get("fallback") == "text_only":
        bias = (
            f"Multimodal requested but fell back to text-only "
            f"(vision_error={mm_meta.get('vision_error')}); "
            f"{n_shots} screenshot label(s) and {n_files} output file(s)."
        )
    else:
        bias = (
            f"Judge packet includes {n_shots} screenshot label(s) and {n_files} output file(s) "
            "(text labels/previews; image bytes not attached)."
        )

    if parsed is None or response is None:
        return {
            "task_id": task.id,
            "error": f"{type(last_error).__name__}: {last_error}" if last_error else "judge_failed",
            "scores": {
                "score": None,
                "item_credit": None,
                "full_score": None,
                "assessable_only": None,
            },
            "evidence_notes": {
                "screenshots": f"{n_shots} labeled" if n_shots else "empty",
                "output_files": f"{n_files} files" if n_files else "empty",
                "bias": "Judge failed after clipped retry; agent artifacts unchanged.",
                "multimodal": mm_meta,
            },
            "multimodal": mm_meta,
            "judge_model": model,
            "judge_reasoning_effort": reasoning_effort,
            "judge_cost_usd": total_cost if total_cost else None,
            "judge_cost_known": total_cost is not None,
            "judge_raw_text": last_raw,
        }

    scoring = score_judgment(task, parsed)
    # Prefer accumulated cost when multimodal+fallback made multiple calls.
    judge_cost = total_cost if total_cost is not None else response.cost_usd
    return {
        "task_id": task.id,
        "judgment": parsed.model_dump(),
        "scores": {
            "score": scoring["score"],
            "item_credit": scoring["item_credit"],
            "full_score": scoring["score"],
            "assessable_only": scoring["assessable_score"],
        },
        "scoring": scoring,
        "evidence_notes": {
            "screenshots": (
                f"{n_shots} labeled, {n_attached} images attached"
                if n_attached
                else (f"{n_shots} labeled" if n_shots else "empty")
            ),
            "output_files": f"{n_files} files" if n_files else "empty",
            "bias": bias,
            "multimodal": mm_meta,
        },
        "multimodal": mm_meta,
        "judge_model": model,
        "judge_reasoning_effort": reasoning_effort,
        "judge_cost_usd": judge_cost,
        "judge_cost_known": judge_cost is not None,
        "judge_raw_text": response.text,
    }
