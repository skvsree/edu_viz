#!/usr/bin/env python3
"""
Generate MCQs from flashcard data using AI.

Usage:
    python generate_mcqs.py --input flashcards.txt --output mcqs.json
    python generate_mcqs.py --input flashcards.txt --provider minimax --api-key <key>

Supports: openai, minimax, claude
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add app to path for AI services
sys.path.insert(0, str(Path(__file__).parent))


def load_flashcards(path: str) -> list[dict]:
    """Load flashcards from file. Supports .txt (front|back per line) or .json."""
    path = Path(path)
    if path.suffix == ".json":
        with open(path) as f:
            return json.load(f)

    # Plain text: front|back per line, blank line separates cards
    cards = []
    with open(path) as f:
        content = f.read().strip()

    for block in content.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        if len(lines) >= 2:
            cards.append({"front": lines[0].strip(), "back": lines[1].strip()})
        elif "|" in block:
            parts = block.split("|")
            if len(parts) >= 2:
                cards.append({"front": parts[0].strip(), "back": parts[1].strip()})
    return cards


def build_prompt(text: str) -> str:
    return (
        "You are preparing study material for Indian medical entrance exam revision. "
        "Return strict JSON with keys flashcards and mcqs. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        "Generate a comprehensive, high-value study pack from this source text. "
        "Include as many useful flashcards and MCQs as the material naturally supports without padding or repetition. "
        "Cover key definitions, mechanisms, cause-effect links, comparisons, classifications, formulas, and exam-relevant traps. "
        "Make the MCQs NEET-style, concept-focused, and not trivial.\n\n"
        f"{text}"
    )


def parse_response(raw: str) -> dict:
    import json as _json
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        # Try extracting JSON from markdown code blocks
        for start in ["```json", "```"]:
            if start in raw:
                raw = raw.split(start, 1)[1].split("```", 1)[0].strip()
                try:
                    return _json.loads(raw)
                except _json.JSONDecodeError:
                    pass
        raise ValueError(f"Could not parse JSON from response: {raw[:200]}")


def generate_openai(text: str, api_key: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=build_prompt(text),
    )
    return parse_response(response.output_text)


def generate_minimax(text: str, api_key: str) -> dict:
    import requests
    response = requests.post(
        "https://api.minimax.chat/v1/text/chatcompletion_pro",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "MiniMax-Text-01",
            "messages": [{"role": "user", "content": build_prompt(text)}],
            "max_tokens": 8192,
            "temperature": 0.7,
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("Minimax returned empty response.")
    return parse_response(content)


def generate_claude(text: str, api_key: str) -> dict:
    import requests
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": build_prompt(text)}],
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Claude API error: {response.status_code} - {response.text[:200]}")
    data = response.json()
    content = data.get("content", [{}])[0].get("text", "")
    if not content:
        raise RuntimeError("Claude returned empty response.")
    return parse_response(content)


PROVIDERS = {
    "openai": generate_openai,
    "minimax": generate_minimax,
    "claude": generate_claude,
}


def main():
    parser = argparse.ArgumentParser(description="Generate MCQs from flashcards using AI")
    parser.add_argument("--input", "-i", required=True, help="Input flashcard file (.txt or .json)")
    parser.add_argument("--output", "-o", default="mcqs.json", help="Output JSON file (default: mcqs.json)")
    parser.add_argument("--provider", "-p", default=os.environ.get("AI_PROVIDER", "openai"),
                        choices=list(PROVIDERS.keys()), help="AI provider")
    parser.add_argument("--api-key", "-k", default=os.environ.get("AI_API_KEY"),
                        help="API key (or set AI_API_KEY env var)")
    parser.add_argument("--max-chars", "-m", type=int, default=12000, help="Max chars to send (default: 12000)")
    args = parser.parse_args()

    if not args.api_key:
        print("Error: API key required. Pass --api-key or set AI_API_KEY env var.")
        sys.exit(1)

    # Load flashcards
    cards = load_flashcards(args.input)
    print(f"Loaded {len(cards)} flashcards from {args.input}")

    # Build source text
    source_text = "\n\n".join(
        f"Q: {c['front']}\nA: {c['back']}" for c in cards
    )
    if len(source_text) > args.max_chars:
        source_text = source_text[:args.max_chars]
        print(f"Truncated to {args.max_chars} chars")

    # Generate
    print(f"Generating MCQs with {args.provider}...")
    generator = PROVIDERS[args.provider]
    result = generator(source_text, args.api_key)

    # Save output
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    mcqs = result.get("mcqs", [])
    print(f"Generated {len(mcqs)} MCQs -> {args.output}")

    # Print first 3
    for i, q in enumerate(mcqs[:3], 1):
        print(f"\nQ{i}: {q['question']}")
        for j, opt in enumerate(q["options"]):
            marker = " [ANSWER]" if j == q["answer_index"] else ""
            print(f"  {chr(65+j)}. {opt}{marker}")


if __name__ == "__main__":
    main()
