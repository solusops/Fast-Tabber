"""Verify the exact local models before enabling browser capabilities."""

import argparse
import asyncio
import base64
import json
import math
import struct
import zlib
from pathlib import Path

import httpx

OLLAMA = "http://127.0.0.1:11434"
GEMMA = "gemma4:e2b"
EMBEDDING = "embeddinggemma-2:740m"


def require_models(payload: dict) -> dict[str, str]:
    available = {model["name"]: model.get("digest") for model in payload["models"]}
    for name in (GEMMA, EMBEDDING):
        if name not in available:
            raise RuntimeError(f"Required model unavailable: {name}. No substitute is permitted.")
        if not available[name]:
            raise RuntimeError(f"Missing model digest: {name}")
    return {name: available[name] for name in (GEMMA, EMBEDDING)}


def red_image() -> str:
    # A generated fixture tests actual image inference without exposing browser data.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 128) * 128))
    return base64.b64encode(png + chunk(b"IEND", b"")).decode()


async def request(client, path, payload=None):
    response = await client.get(path) if payload is None else await client.post(path, json=payload)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


async def pull_embeddings(client):
    async with client.stream("POST", "/api/pull", json={"name": EMBEDDING, "stream": True}) as response:
        response.raise_for_status()
        previous = None
        async for line in response.aiter_lines():
            item = json.loads(line)
            if item.get("error"):
                raise RuntimeError(item["error"])
            status = item.get("status")
            if status != previous:
                print(status, flush=True)
                previous = status
        if previous != "success":
            raise RuntimeError("Embedding model download did not finish successfully")


async def check_models(client):
    digests = require_models(await request(client, "/api/tags"))
    capabilities = {}
    for name, required in ((GEMMA, {"completion", "tools", "vision"}), (EMBEDDING, {"embedding"})):
        info = await request(client, "/api/show", {"model": name})
        missing = required - set(info.get("capabilities", []))
        if missing:
            raise RuntimeError(f"{name} lacks required capabilities: {sorted(missing)}")
        capabilities[name] = info["capabilities"]

    common = {"model": GEMMA, "stream": False, "options": {"num_ctx": 8192, "num_predict": 256}}
    reply = await request(client, "/api/chat", common | {
        "messages": [{"role": "user", "content": "Reply with exactly READY."}],
    })
    if reply.get("message", {}).get("content", "").strip().rstrip(".") != "READY":
        raise RuntimeError("Gemma text execution failed the READY check")
    print("PASS local text generation", flush=True)

    tool = await request(client, "/api/chat", common | {
        "messages": [{"role": "user", "content": "Call check_local_runtime with no arguments now. Do not answer in text."}],
        "tools": [{"type": "function", "function": {
            "name": "check_local_runtime", "description": "Check the local inference runtime.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }}],
    })
    calls = tool.get("message", {}).get("tool_calls", [])
    if len(calls) != 1 or calls[0]["function"]["name"] != "check_local_runtime" or calls[0]["function"].get("arguments") != {}:
        raise RuntimeError("Gemma structured tool calling failed; browser operations remain disabled")
    print("PASS structured tool selection (agent round-trip still requires Chrome)", flush=True)

    vision = await request(client, "/api/chat", common | {
        "format": {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]},
        "messages": [{"role": "user", "images": [red_image()], "content": "Identify the dominant color of this image. Return JSON with color."}],
    })
    if json.loads(vision["message"]["content"])["color"].lower().strip() != "red":
        raise RuntimeError("Gemma image inference failed the visual fixture")
    print("PASS local image interpretation", flush=True)

    result = await request(client, "/api/embed", {"model": EMBEDDING, "input": ["robot learning", "university admissions"], "truncate": False})
    vectors = result.get("embeddings", [])
    if len(vectors) != 2 or not vectors[0] or len(vectors[0]) != len(vectors[1]) or not all(math.isfinite(x) for v in vectors for x in v) or vectors[0] == vectors[1]:
        raise RuntimeError("EmbeddingGemma returned invalid embeddings")
    print(f"PASS local embeddings ({len(vectors[0])} dimensions)", flush=True)
    return {"models": digests, "capabilities": capabilities, "embedding_dimensions": len(vectors[0]), "external_inference_block_test": "not yet performed"}


def save_report(report: dict, state: Path):
    state.mkdir(exist_ok=True)
    report_path = state / "models.json"
    if report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        if previous.get("models") == report["models"] and previous.get("external_inference_block_test", "").startswith("passed:"):
            report["external_inference_block_test"] = previous["external_inference_block_test"]
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-embeddings", action="store_true")
    args = parser.parse_args()
    async with httpx.AsyncClient(base_url=OLLAMA, timeout=600, trust_env=False) as client:
        version = await request(client, "/api/version")
        print(f"Ollama {version['version']}", flush=True)
        if args.pull_embeddings:
            await pull_embeddings(client)
        report = await check_models(client)
    save_report(report, Path(".state"))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
