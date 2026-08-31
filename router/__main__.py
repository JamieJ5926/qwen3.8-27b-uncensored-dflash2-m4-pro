"""Executable router entrypoint. Serves or replays; never manages model processes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from router.artifacts import ArtifactStore
from router.coordinator import Coordinator, FixedWorkerAdapter, replay_final_response
from router.domain import RuntimeConfig
from router.http_boundary import RouterHTTPServer, final_to_openai
from router.rules import load_route_table_path
from router.targets import Adapter
from router.transport import (
    DEFAULT_LFM_MODEL,
    DEFAULT_QWEN_BASE_URL,
    LFM_BASE_URL_ENV,
    LFM_MODEL_ENV,
    QWEN_BASE_URL_ENV,
    LFMTransport,
    QwenTransport,
)

DEFAULT_RULES_PATH = Path("router/fixtures/routes/default.json")
DEFAULT_ARTIFACTS_DIR = Path("router-artifacts")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8060


def resolve_qwen_base_url(
    explicit: str | None, env: Mapping[str, str] | None = None
) -> str:
    environ = os.environ if env is None else env
    return explicit or environ.get(QWEN_BASE_URL_ENV) or DEFAULT_QWEN_BASE_URL


def resolve_lfm_base_url(
    explicit: str | None, env: Mapping[str, str] | None = None
) -> str:
    environ = os.environ if env is None else env
    return explicit or environ.get(LFM_BASE_URL_ENV, "")


def resolve_lfm_model(
    explicit: str | None, env: Mapping[str, str] | None = None
) -> str:
    environ = os.environ if env is None else env
    return explicit or environ.get(LFM_MODEL_ENV) or DEFAULT_LFM_MODEL


def build_handle_chat(
    *,
    config: RuntimeConfig,
    artifacts: ArtifactStore,
    qwen_transport: QwenTransport | None = None,
    lfm_transport: LFMTransport | None = None,
    adapters: MutableMapping[str, Adapter] | None = None,
    fixed_workers: MutableMapping[str, FixedWorkerAdapter] | None = None,
    timeout_s: float = 60.0,
    lfm_model: str = DEFAULT_LFM_MODEL,
) -> Coordinator:
    qwen = qwen_transport or QwenTransport(
        base_url=config.qwen_base_url,
        timeout_s=timeout_s,
    )
    lfm = lfm_transport
    if lfm is None and config.lfm_base_url:
        lfm = LFMTransport(
            base_url=config.lfm_base_url,
            model=lfm_model,
            timeout_s=timeout_s,
        )
    return Coordinator(
        transport=qwen,
        lfm_transport=lfm,
        artifacts=artifacts,
        adapters=adapters,
        fixed_workers=fixed_workers,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve the OpenAI-compatible boundary")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    serve.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    serve.add_argument("--qwen-url", default=None)
    serve.add_argument("--lfm-url", default=None)
    serve.add_argument("--lfm-model", default=None)
    serve.add_argument("--timeout-s", type=float, default=60.0)

    replay = sub.add_parser("replay", help="rebuild a stored result without a model")
    replay.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    replay.add_argument("--request-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "replay":
        try:
            final = replay_final_response(ArtifactStore(args.artifacts), args.request_id)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(final_to_openai(final, model="router")))
        return 0

    rules = load_route_table_path(args.rules)[1]
    config = RuntimeConfig(
        qwen_base_url=resolve_qwen_base_url(args.qwen_url),
        lfm_base_url=resolve_lfm_base_url(args.lfm_url),
        worker_base_url=os.environ.get("WORKER_BASE_URL", ""),
    )
    coordinator = build_handle_chat(
        config=config,
        artifacts=ArtifactStore(args.artifacts),
        timeout_s=args.timeout_s,
        lfm_model=resolve_lfm_model(args.lfm_model),
    )
    server = RouterHTTPServer(
        handle_chat=coordinator,
        config=config,
        rules=rules,
        host=args.host,
        port=args.port,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
