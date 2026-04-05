from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class AIGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class GeneratedFlashcard:
    front: str
    back: str


@dataclass(slots=True)
class GeneratedMcq:
    question: str
    options: list[str]
    answer_index: int
    explanation: str


@dataclass(slots=True)
class GeneratedStudyPack:
    flashcards: list[GeneratedFlashcard]
    mcqs: list[GeneratedMcq]


@dataclass(slots=True)
class AICredential:
    provider: str
    auth_type: str
    secret: str
    refresh_token: str | None = None
    source: str = "env"


class StudyPackProvider(Protocol):
    name: str

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack: ...


class OpenAIStudyPackProvider:
    name = "openai"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "openai":
            raise AIGenerationError("OpenAI credential is required.")
        if credential.auth_type not in {"api_key", "oauth"}:
            raise AIGenerationError(f"Unsupported OpenAI auth type: {credential.auth_type}")

        from openai import OpenAI

        client = OpenAI(api_key=credential.secret)
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=_build_prompt(text),
        )
        return _parse_study_pack_json(response.output_text)


class MinimaxStudyPackProvider:
    name = "minimax"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "minimax":
            raise AIGenerationError("Minimax credential is required.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported Minimax auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.minimax.io/v1/text/chatcompletion_v2",
            headers={
                "Authorization": f"Bearer {credential.secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-M2",
                "messages": [
                    {"role": "system", "content": "Answer briefly. Do not explain reasoning. Give only final answer in JSON."},
                    {"role": "user", "content": _build_prompt(text)},
                ],
                "max_completion_tokens": 8192,
                "temperature": 0.2,
            },
            timeout=180,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise AIGenerationError("Minimax returned empty response.")
        return _parse_study_pack_json(content)


class ClaudeStudyPackProvider:
    name = "claude"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "claude":
            raise AIGenerationError("Claude credential is required.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported Claude auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": credential.secret,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": _build_prompt(text)}],
            },
            timeout=120,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Claude API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("content", [{}])[0].get("text", "")
        if not content:
            raise AIGenerationError("Claude returned empty response.")
        return _parse_study_pack_json(content)


def _build_prompt(text: str, num_flashcards: int = 3, num_mcqs: int = 5) -> str:
    sample = text[:10000]
    return (
        "You are preparing study material for Indian medical entrance exam revision. "
        "Return strict JSON with keys flashcards and mcqs. "
        f"Generate exactly {num_flashcards} flashcards and exactly {num_mcqs} MCQs. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        "Cover key definitions, mechanisms, cause-effect links, comparisons, classifications, formulas, and exam-relevant traps. "
        "Make the MCQs NEET-style, concept-focused, and not trivial.\n\n"
        f"{sample}"
    )


def _parse_study_pack_json(raw: str) -> GeneratedStudyPack:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIGenerationError("AI provider returned invalid JSON for study generation.") from exc

    flashcards = [
        GeneratedFlashcard(front=item["front"].strip(), back=item["back"].strip())
        for item in data.get("flashcards", [])
        if item.get("front") and item.get("back")
    ]
    mcqs: list[GeneratedMcq] = []
    for item in data.get("mcqs", []):
        options = [str(opt).strip() for opt in item.get("options", []) if str(opt).strip()]
        answer_index = int(item.get("answer_index", -1))
        if item.get("question") and len(options) == 4 and answer_index in {0, 1, 2, 3}:
            mcqs.append(
                GeneratedMcq(
                    question=item["question"].strip(),
                    options=options,
                    answer_index=answer_index,
                    explanation=str(item.get("explanation", "")).strip(),
                )
            )

    if not flashcards and not mcqs:
        raise AIGenerationError("AI provider did not return usable flashcards or MCQs.")
    return GeneratedStudyPack(flashcards=flashcards, mcqs=mcqs)


def get_study_pack_provider(name: str) -> StudyPackProvider:
    name = name.strip().lower()
    if name == "openai":
        return OpenAIStudyPackProvider()
    if name == "minimax":
        return MinimaxStudyPackProvider()
    if name == "claude":
        return ClaudeStudyPackProvider()
    if name == "opencode":
        return OpencodeStudyPackProvider()
    raise AIGenerationError(f"Unsupported AI study pack provider: {name}")




class OpencodeStudyPackProvider:
    """OpenCode provider using MiniMax API."""
    name = "opencode"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "minimax":
            raise AIGenerationError("Minimax credential is required for opencode provider.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.minimax.io/v1/text/chatcompletion_v2",
            headers={
                "Authorization": f"Bearer {credential.secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-M2",
                "messages": [
                    {"role": "system", "content": "Answer briefly. Do not explain reasoning. Give only final answer in JSON."},
                    {"role": "user", "content": _build_prompt(text)},
                ],
                "max_completion_tokens": 8192,
                "temperature": 0.2,
            },
            timeout=180,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise AIGenerationError("Minimax returned empty response.")
        return _parse_study_pack_json(content)



class CodexStudyPackProvider:
    """Codex CLI provider for study pack generation."""
    name = "codex"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        import subprocess
        import tempfile
        import os
        
        prompt = _build_prompt(text)
        
        # Create temp file with prompt
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        
        try:
            result = subprocess.run(
                ["codex", "exec", f"Return JSON only (no explanation): {prompt}"],
                cwd="/opt/edu_viz",
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = result.stdout
            if not output:
                output = result.stderr
        except subprocess.TimeoutExpired:
            raise AIGenerationError("Codex CLI timed out.")
        except FileNotFoundError:
            raise AIGenerationError("Codex CLI not found. Install with: npm install -g @openai/codex")
        finally:
            os.unlink(prompt_file)
        
        if not output or "error" in output.lower():
            raise AIGenerationError(f"Codex CLI error: {output[:200]}")
        
        return _parse_study_pack_json(output)


def generate_study_pack(text: str, *, provider_name: str, credential: AICredential) -> GeneratedStudyPack:
    return get_study_pack_provider(provider_name).generate(text, credential)
