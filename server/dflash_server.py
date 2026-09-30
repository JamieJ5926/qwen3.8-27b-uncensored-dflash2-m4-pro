"""Stock DFlash OpenAI-compatible server.

Reuses mlx_lm.server's stdlib HTTP/OpenAI machinery (0.31.3) via subclassing and
swaps the generation core for dflash 0.1.0's stock ``model_mlx.stream_generate``.
Single serialized generation worker (provider reports is_batchable=False, so
every request flows through _serve_single on the one worker thread, which also
keeps dflash's global GDN class-patch uncontended).

Endpoints: GET /health, GET /v1/models, POST /v1/chat/completions,
POST /v1/completions, GET /v1/dflash/stats.

All metrics come from real GenerationResponse yields; none are constants.
"""

import argparse
import json
import logging
import os
import threading
import time
from collections import deque

import mlx.core as mx
from mlx_lm.models.cache import LRUPromptCache
from mlx_lm.server import (
    APIHandler,
    GenerationContext,
    ModelProvider,
    Response,
    ResponseGenerator,
    _run_http_server,
)

import dflash.model_mlx as dflash_mlx
from dflash.benchmark import load_mlx_models

DEFAULT_TARGET = "/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit"
DEFAULT_DRAFT = "incoai/Qwen3.8-27B-DFlash2"
DEFAULT_MODEL_NAME = "qwen3.8-27b"

_METAL_POISON_MARKERS = (
    "timeout",
    "submissionsignored",
    "submission ignored",
    "command buffer",
    "metal",
)


def _is_metal_poison(err: Exception) -> bool:
    msg = str(err).lower()
    return any(marker in msg for marker in _METAL_POISON_MARKERS)


class DFlashModelProvider(ModelProvider):
    """Loads the stock target + DFlash2 draft pair once; never reloads."""

    def __init__(self, cli_args):
        super().__init__(cli_args)
        self.loaded = False
        self.poisoned = False

    def load_default(self):
        logging.info(
            "Loading target %s (draft %s, bits %s)...",
            self.cli_args.model,
            self.cli_args.draft,
            self.cli_args.draft_bits,
        )
        tic = time.perf_counter()
        model, draft, tokenizer = load_mlx_models(
            self.cli_args.model, self.cli_args.draft, self.cli_args.draft_bits
        )
        self.model_key = (self.cli_args.model, None, None)
        self.model = model
        self.tokenizer = tokenizer
        self.draft_model = draft
        self.is_batchable = False
        self.loaded = True
        logging.info(
            "MODEL READY: target+draft resident in %.1fs", time.perf_counter() - tic
        )

    def load(self, model_path, adapter_path=None, draft_model_path=None):
        # Single-tenant: every requested model id maps to the resident pair.
        return self.model, self.tokenizer


class DFlashResponseGenerator(ResponseGenerator):
    def __init__(self, model_provider, prompt_cache):
        self.stats_lock = threading.Lock()
        self.stats = {
            "requests": 0,
            "errors": 0,
            "total_completion_tokens": 0,
            "acceptance_lengths": deque(maxlen=2000),
            "recent": deque(maxlen=100),
            "last_error": None,
        }
        self._deadline = None
        super().__init__(model_provider, prompt_cache)
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        """Fail closed on a generation hang; hub restart recovers the process."""
        while True:
            deadline = self._deadline
            if deadline is not None and time.time() > deadline:
                logging.critical(
                    "WATCHDOG: generation exceeded %.0fs deadline; exiting for restart",
                    self.cli_args.watchdog_seconds,
                )
                os._exit(1)
            time.sleep(15)

    def _record(self, metrics, error=None):
        with self.stats_lock:
            s = self.stats
            if error is not None:
                s["errors"] += 1
                s["last_error"] = f"{type(error).__name__}: {error}"
                return
            s["requests"] += 1
            s["total_completion_tokens"] += metrics.get("completion_tokens", 0)
            s["acceptance_lengths"].extend(metrics.get("acceptance_lengths", []))
            s["recent"].append(metrics)
        logging.info("DFLASH_REQUEST %s", json.dumps(metrics, default=str))

    def _serve_single(self, wrapped):
        rqueue, request, args = wrapped
        metrics = getattr(request, "dflash_metrics", None)
        t_start = time.perf_counter()
        if metrics is not None:
            enqueued = metrics.pop("enqueued", None)
            if enqueued is not None:
                metrics["queue_wait_seconds"] = round(time.time() - enqueued, 3)
        args.max_tokens = min(args.max_tokens, self.cli_args.request_max_tokens)
        try:
            provider = self.model_provider
            tokenizer = provider.tokenizer
            # TokenizerWrapper.detokenizer is a property returning a fresh
            # streaming detokenizer per access; make our own for per-token
            # incremental text.
            det = type(tokenizer.detokenizer)(tokenizer)
            prompt, _, _, initial_state = self._tokenize(tokenizer, request, args)
            if len(prompt) > self.cli_args.request_max_prompt_tokens:
                raise ValueError(
                    f"prompt has {len(prompt)} tokens; limit is "
                    f"{self.cli_args.request_max_prompt_tokens}"
                )
            sm, sequences = self._make_state_machine(
                provider.model_key, tokenizer, args.stop_words,
                initial_state=initial_state,
            )
            sm_state = sm.make_state()
            ctx = GenerationContext(
                has_thinking=tokenizer.has_thinking,
                has_tool_calling=tokenizer.has_tool_calling,
                tool_parser=tokenizer.tool_parser,
                sequences=sequences,
                prompt=prompt,
            )
            rqueue.put(ctx)
            if args.seed is not None:
                mx.random.seed(args.seed)

            self._deadline = time.time() + self.cli_args.watchdog_seconds
            block_size = self.cli_args.block_size
            if metrics is not None:
                metrics["block_size"] = block_size

            accepted_lengths = []
            completion_tokens = 0
            finish_reason = "length"
            gen = dflash_mlx.stream_generate(
                provider.model, provider.draft_model, tokenizer, prompt,
                block_size=block_size,
                max_tokens=args.max_tokens,
                temperature=args.sampling.temperature,
                top_p=args.sampling.top_p,
                top_k=args.sampling.top_k,
                prefill_step_size=self.cli_args.prefill_step_size,
            )
            block = next(gen, None)
            last_block = None
            while block is not None:
                nxt = next(gen, None)
                # finish_reason arrives on the block carrying it; the final
                # "length" block has no tokens, so it applies to the previous
                # token only under lookahead.
                fr = (
                    block.finish_reason
                    if (nxt is None or block.finish_reason is not None)
                    else None
                )
                last = len(block.tokens) - 1
                aborted = False
                for j, token in enumerate(block.tokens):
                    det.add_token(token)
                    sm_state, match_seq, cur_state = sm.match(sm_state, token)
                    f = fr if j == last else None
                    if match_seq is not None and cur_state is None:
                        f = "stop"
                    rqueue.put(
                        Response(
                            det.last_segment, token, cur_state, match_seq,
                            0.0, f, (),
                        )
                    )
                    completion_tokens += 1
                    if f is not None:
                        finish_reason = f
                    if ctx._should_stop:
                        aborted = True
                        break
                if block.accepted is not None:
                    accepted_lengths.append(block.accepted)
                if aborted:
                    gen.close()
                    break
                last_block = block
                block = nxt

            if metrics is not None:
                metrics.update(
                    prompt_tokens=len(prompt),
                    completion_tokens=completion_tokens,
                    acceptance_lengths=accepted_lengths,
                    mean_acceptance=(
                        round(sum(accepted_lengths) / len(accepted_lengths), 3)
                        if accepted_lengths
                        else None
                    ),
                    generation_tps=(
                        round(last_block.generation_tps, 2) if last_block else None
                    ),
                    prefill_tps=round(last_block.prompt_tps, 2) if last_block else None,
                    peak_memory_gb=(
                        round(last_block.peak_memory, 2) if last_block else None
                    ),
                    wall_seconds=round(time.perf_counter() - t_start, 3),
                    finish_reason=finish_reason,
                )
            rqueue.put(None)
            self._record(metrics)
        except Exception as e:
            logging.exception("Generation failed")
            rqueue.put(e)
            self._record(metrics or {}, error=e)
            if _is_metal_poison(e):
                self.model_provider.poisoned = True
                logging.critical(
                    "POISONED METAL STATE (%s: %s); exiting for supervisor restart",
                    type(e).__name__, e,
                )
                threading.Timer(1.0, os._exit, (1,)).start()
        finally:
            self._deadline = None


class DFlashAPIHandler(APIHandler):
    def do_POST(self):
        try:
            super().do_POST()
        except (ValueError, AssertionError) as e:
            self._set_completion_headers(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_chat_completions(self):
        request = super().handle_chat_completions()
        self.dflash_metrics = {"enqueued": time.time()}
        request.dflash_metrics = self.dflash_metrics
        return request

    def handle_text_completions(self):
        request = super().handle_text_completions()
        self.dflash_metrics = {"enqueued": time.time()}
        request.dflash_metrics = self.dflash_metrics
        return request

    def validate_model_parameters(self):
        super().validate_model_parameters()
        # dflash's stock sampler supports only temperature/top_p/top_k.
        unsupported = []
        if self.min_p:
            unsupported.append("min_p")
        if self.xtc_probability or self.xtc_threshold:
            unsupported.append("xtc_probability/xtc_threshold")
        if self.repetition_penalty:
            unsupported.append("repetition_penalty")
        if self.presence_penalty:
            unsupported.append("presence_penalty")
        if self.frequency_penalty:
            unsupported.append("frequency_penalty")
        if self.logit_bias:
            unsupported.append("logit_bias")
        if self.logprobs or self.top_logprobs > 0:
            unsupported.append("logprobs/top_logprobs")
        if unsupported:
            raise ValueError(
                "Unsupported parameters for stock DFlash server: "
                + ", ".join(unsupported)
            )

    def generate_response(self, text, finish_reason, *args, **kwargs):
        response = super().generate_response(text, finish_reason, *args, **kwargs)
        if finish_reason is not None:
            metrics = getattr(self, "dflash_metrics", None)
            if metrics:
                response["dflash"] = {
                    k: v for k, v in metrics.items() if k != "enqueued"
                }
        return response

    def handle_models_request(self):
        self._set_completion_headers(200)
        self.end_headers()
        model_name = self.response_generator.cli_args.model_name
        body = {
            "object": "list",
            "data": [
                {"id": model_name, "object": "model", "created": self.created}
            ],
        }
        self.wfile.write(json.dumps(body).encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/v1/dflash/stats":
            self.handle_dflash_stats()
        elif self.path == "/health":
            self.handle_dflash_health()
        else:
            super().do_GET()

    def handle_dflash_health(self):
        provider = self.response_generator.model_provider
        if provider.poisoned:
            status = "poisoned"
        elif provider.loaded:
            status = "ready"
        else:
            status = "loading"
        self._set_completion_headers(200 if status == "ready" else 503)
        self.end_headers()
        self.wfile.write(json.dumps({"status": status}).encode())
        self.wfile.flush()

    def handle_dflash_stats(self):
        gen = self.response_generator
        with gen.stats_lock:
            s = gen.stats
            acc = list(s["acceptance_lengths"])
            body = {
                "model": gen.cli_args.model,
                "draft": gen.cli_args.draft,
                "draft_bits": gen.cli_args.draft_bits,
                "block_size": gen.cli_args.block_size,
                "requests": s["requests"],
                "errors": s["errors"],
                "total_completion_tokens": s["total_completion_tokens"],
                "acceptance_histogram": {
                    str(b): acc.count(b) for b in range(gen.cli_args.block_size + 1)
                    if acc.count(b)
                },
                "mean_acceptance": (
                    round(sum(acc) / len(acc), 3) if acc else None
                ),
                "last_error": s["last_error"],
                "recent_requests": list(s["recent"]),
                "uptime_seconds": round(time.time() - START_TIME, 1),
            }
        self._set_completion_headers(200)
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())
        self.wfile.flush()


START_TIME = time.time()


def build_parser():
    parser = argparse.ArgumentParser(description="Stock DFlash OpenAI server.")
    parser.add_argument("--model", type=str, default=DEFAULT_TARGET)
    parser.add_argument("--draft", type=str, default=DEFAULT_DRAFT)
    parser.add_argument("--draft-bits", type=int, default=4)
    parser.add_argument("--block-size", type=int, default=5)
    parser.add_argument("--model-name", type=str, default=DEFAULT_MODEL_NAME)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--watchdog-seconds", type=float, default=600.0)
    parser.add_argument("--request-max-prompt-tokens", type=int, default=16384)
    parser.add_argument("--request-max-tokens", type=int, default=1024)
    parser.add_argument("--log-level", type=str, default="INFO")
    # Args consumed by the inherited mlx_lm.server machinery.
    parser.add_argument("--adapter-path", type=str, default=None)
    parser.add_argument("--draft-model", type=str, default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--chat-template", type=str, default="")
    parser.add_argument("--use-default-chat-template", action="store_true")
    parser.add_argument("--temp", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument("--min-p", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--num-draft-tokens", type=int, default=3)
    parser.add_argument(
        "--chat-template-args", type=json.loads, default='{"enable_thinking": false}'
    )
    parser.add_argument("--allowed-origins", type=lambda x: x.split(","), default=["*"])
    parser.add_argument("--decode-concurrency", type=int, default=32)
    parser.add_argument("--prompt-concurrency", type=int, default=8)
    parser.add_argument("--prefill-step-size", type=int, default=2048)
    parser.add_argument("--prompt-cache-size", type=int, default=10)
    parser.add_argument("--prompt-cache-bytes", type=str, default=None)
    parser.add_argument("--pipeline", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if args.watchdog_seconds <= 0:
        raise SystemExit("--watchdog-seconds must be greater than 0")
    if args.request_max_prompt_tokens <= 0:
        raise SystemExit("--request-max-prompt-tokens must be greater than 0")
    if args.request_max_tokens <= 0:
        raise SystemExit("--request-max-tokens must be greater than 0")
    if mx.metal.is_available():
        mx.set_wired_limit(mx.device_info()["max_recommended_working_set_size"])
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), None),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    provider = DFlashModelProvider(args)
    generator = DFlashResponseGenerator(provider, LRUPromptCache(args.prompt_cache_size))
    _run_http_server(args.host, args.port, generator, handler_class=DFlashAPIHandler)


if __name__ == "__main__":
    main()
