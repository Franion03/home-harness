"""Provider registry and the router that walks a route's fallback chain.

This is the only place that knows a model reference like
"anthropic/claude-opus-5" maps to a particular adapter. Everything downstream
sees a Completion and cannot tell which vendor produced it.
"""

from __future__ import annotations

import logging

from config import RouteConfig, Settings
from gateway import Gateway
from provider_anthropic import AnthropicProvider
from provider_base import Completion, Message, Provider, ProviderError, ToolSpec
from provider_google import GoogleProvider
from provider_openai import OpenAICompatProvider

log = logging.getLogger("harness.llm")


def split_ref(ref: str) -> tuple[str, str]:
    """'anthropic/claude-opus-5' -> ('anthropic', 'claude-opus-5').

    Split on the first slash only: OpenRouter model ids contain slashes of
    their own ('openrouter/google/gemini-2.0-flash-001').
    """
    provider, _, model = ref.partition("/")
    if not model:
        raise ProviderError(
            f"model reference '{ref}' must be 'provider/model'", retryable=False
        )
    return provider, model


def build_registry(settings: Settings, gateway: Gateway) -> dict[str, Provider]:
    """Instantiate one adapter per vendor slug.

    Adding a vendor is one entry here. Vendors with no key configured are still
    registered -- they raise a clear ProviderError if a route names them.
    """
    return {
        "anthropic": AnthropicProvider(gateway, settings.anthropic_key),
        "google-ai-studio": GoogleProvider(gateway, settings.google_key),
        "openai": OpenAICompatProvider(gateway, settings.openai_key, slug="openai"),
        "openrouter": OpenAICompatProvider(
            gateway,
            settings.openrouter_key,
            slug="openrouter",
            extra_headers={
                "HTTP-Referer": "https://github.com/Franion03/home-harness",
                "X-Title": "home-harness",
            },
        ),
        "groq": OpenAICompatProvider(gateway, settings.groq_key, slug="groq"),
        "workers-ai": OpenAICompatProvider(
            gateway, settings.workers_ai_key, slug="workers-ai"
        ),
    }


class Router:
    """Runs a request against a route's primary model, then its fallback."""

    def __init__(self, settings: Settings, registry: dict[str, Provider]):
        self._settings = settings
        self._registry = registry

    async def complete(
        self,
        route: RouteConfig,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Completion:
        chain = route.chain
        if not chain:
            raise ProviderError("route has no primary model configured", retryable=False)

        last: ProviderError | None = None
        for ref in chain:
            provider_slug, model = split_ref(ref)
            provider = self._registry.get(provider_slug)
            if provider is None:
                last = ProviderError(
                    f"unknown provider '{provider_slug}' in route", retryable=False
                )
                log.error("%s", last)
                continue

            # Route-level options (thinking, effort) are provider-specific and
            # only meaningful to the adapter that understands them.
            if route.options:
                provider = _with_options(provider, route.options)

            try:
                completion = await provider.complete(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=route.max_tokens,
                    temperature=route.temperature,
                )
                if ref != chain[0]:
                    log.info("served by fallback %s", ref)
                return completion
            except ProviderError as exc:
                last = exc
                if not exc.retryable:
                    log.error("%s failed unrecoverably: %s", ref, exc)
                    break
                log.warning("%s failed (%s) -- trying next in chain", ref, exc)

        raise last or ProviderError("every model in the route failed")


def _with_options(provider: Provider, options: dict) -> Provider:
    """Return a copy of the adapter carrying this route's options.

    Adapters are cheap value objects, so a shallow rebuild is simpler than
    threading options through every call signature.
    """
    if isinstance(provider, AnthropicProvider):
        return AnthropicProvider(provider._gw, provider._key, options=options)
    if isinstance(provider, GoogleProvider):
        return GoogleProvider(provider._gw, provider._key, options=options)
    if isinstance(provider, OpenAICompatProvider):
        return OpenAICompatProvider(
            provider._gw,
            provider._key,
            slug=provider.slug,
            path=provider._path,
            extra_headers=provider._extra_headers,
            options=options,
        )
    return provider
