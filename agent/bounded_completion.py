"""One-shot completion contract, independent of presence decision/state owners.

Limits describe transport facts, not universal provider guarantees. Request mode
bounds attempts, time and received bytes, NOT server tokens/cost/hidden reasoning.
The caller must durably reserve its profile quota before calling complete().
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import json
import math
from urllib.parse import urlparse


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    base_url: str | None
    mode: str
    api_mode: str | None = None
    runtime: str | None = None
    configured: bool = False


@dataclass(frozen=True)
class Capabilities:
    transport: str
    server_output_cap: str
    cancellation: str = 'close_connection'
    usage: str = 'post_hoc_or_unknown'
    output_limit_parameter: str | None = 'max_tokens'
    native_async: bool = True
    max_physical_requests: int = 1
    redirects: bool = False
    adapter_version: str = 'bounded-completion-v1'


@dataclass(frozen=True)
class CompletionLimits:
    requested_output_tokens: int
    deadline_seconds: float
    server_token_guarantee: bool
    max_physical_requests: int = 1
    max_input_bytes: int = 6144
    max_response_bytes: int = 16384


@dataclass(frozen=True)
class CompletionResult:
    text: str
    route: Route
    capabilities: Capabilities
    mode: str
    usage: str
    output_tokens: int | None
    limits: CompletionLimits
    # Includes reasoning only when the endpoint reports total generated tokens.
    reasoning_tokens: int | None = None
    authority: object = field(default=None, repr=False, compare=False)


class CompletionHeld(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('unsupported bounded presence route: '+reason)


def select_route(config: dict) -> Route:
    """Explicit task override, otherwise configured main route; never detection.

    A different task provider must name its model. Main model inheritance occurs
    only for the same provider. No catalog default or fallback model is selected.
    """
    main = config.get('model', {})
    if isinstance(main, str):
        main = {'default': main, 'provider': config.get('provider')}
    task = config.get('auxiliary', {}).get('presence', {})
    if not isinstance(main, dict) or not isinstance(task, dict):
        raise CompletionHeld('invalid_configured_route')
    primary = main.get('provider')
    provider = task.get('provider')
    inherited = provider in (None, 'auto')
    provider = primary if inherited else provider
    model = task.get('model')
    if model in (None, 'auto'):
        model = (main.get('default') or main.get('model')) if inherited or provider == primary else None
    for value in (provider, model):
        if (not isinstance(value, str) or not value or value == 'auto' or value != value.strip()
                or len(value.encode()) > 512 or any(ord(c) < 32 for c in value)):
            raise CompletionHeld('configured_provider_and_model_required')
    from agent.bounded_provider_metadata import configured_metadata
    owner = configured_metadata(config, provider)
    base = task.get('base_url', main.get('base_url') if inherited or provider == primary else None)
    if base is None and owner is not None:
        base = owner.endpoint
    if base is not None:
        parsed = urlparse(base) if isinstance(base, str) else None
        if (parsed is None or parsed.scheme not in {'https','http'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or base != base.strip()):
            raise CompletionHeld('invalid_configured_endpoint')
    if provider == 'custom' and not base:
        raise CompletionHeld('custom_endpoint_required_no_detection')
    mode = config.get('presence', {}).get('budget_mode', 'strict_tokens')
    if mode not in {'strict_tokens','requests'}:
        raise CompletionHeld('unsupported_budget_mode')
    assert isinstance(provider,str) and isinstance(model,str)
    api_mode = task.get('api_mode', main.get('api_mode') if inherited or provider == primary else None)
    if api_mode is None and owner is not None:
        api_mode = owner.api_mode
    return Route(provider, model, base, mode, api_mode,
                 main.get('openai_runtime') if inherited or provider == primary else None, owner is not None)


def _dispatch_route(config):
    """Enrich only assessment dispatch, never the cold settings/status API."""
    route = select_route(config)
    if route.api_mode is None and not route.configured:
        from clover_cli.models import opencode_provider_family, opencode_model_api_mode
        if opencode_provider_family(route.provider) is not None:
            route = replace(route, api_mode=opencode_model_api_mode(route.provider, route.model))
    return route


def capabilities(route: Route) -> Capabilities:
    """Pure route-metadata lookup, not auth detection or a provider allowlist.

    Recognized cap contracts bind the declared HTTPS endpoint + wire. The exact
    model remains in the immutable Route; serializer inflation and resolved
    endpoint/model changes are independently refused before HTTP dispatch.
    """
    from agent.bounded_provider_metadata import metadata
    owner = metadata({}, route.provider)
    endpoint = route.base_url or owner.endpoint
    wire = route.api_mode or owner.api_mode
    if not route.configured and route.provider in {'openai-codex','codex'}:
        return Capabilities('responses', 'unsupported', output_limit_parameter=None)
    # Cloud SDK/external-process and other OAuth credential owners have no
    # audited native one-shot binding here. Do not resolve/refresh them to guess.
    if not route.configured and owner.auth_type != 'api_key':
        return Capabilities('unsupported', 'unverified', cancellation='unverified', native_async=False)
    parsed = urlparse(endpoint)
    if wire == 'anthropic_messages':
        verified = (parsed.scheme == 'https' and parsed.hostname == 'api.anthropic.com'
                    and parsed.path.rstrip('/') in {'','/v1'})
        return Capabilities('anthropic_messages', 'verified' if verified else 'unverified')
    if route.provider == 'gemini' and not route.configured:
        # Candidate-token count/maxOutputTokens is not an all-generated-token cap.
        return Capabilities('gemini_native', 'unverified', output_limit_parameter='maxOutputTokens')
    if wire == 'codex_responses':
        verified = (parsed.scheme == 'https' and parsed.hostname == 'api.openai.com'
                    and parsed.path.rstrip('/') == '/v1')
        return Capabilities('responses', 'verified' if verified else 'unverified',
                            output_limit_parameter='max_output_tokens' if verified else None)
    if wire == 'chat_completions' and (route.provider in {'openai','custom'} or
            route.configured or owner.auth_type == 'api_key'):
        verified = (parsed.scheme == 'https' and parsed.hostname == 'api.openai.com'
                    and parsed.path.rstrip('/') == '/v1')
        return Capabilities('chat_completions', 'verified' if verified else 'unverified',
                            output_limit_parameter='max_completion_tokens' if verified else 'max_tokens')
    return Capabilities('unsupported', 'unverified', cancellation='unverified', native_async=False)


def admitted(route: Route) -> Capabilities:
    if route.api_mode is not None and route.api_mode not in ('chat_completions','anthropic_messages','codex_responses'):
        raise CompletionHeld('transport_selection_unsupported')
    cap = capabilities(route)
    if cap.transport == 'unsupported':
        raise CompletionHeld('transport_cancellation_unverified')
    if route.mode == 'strict_tokens' and cap.server_output_cap != 'verified':
        if not route.configured and route.provider in {'openai-codex','codex'}:
            from agent.auxiliary_client import PresenceOutputBudgetUnsupported
            raise PresenceOutputBudgetUnsupported('Codex presence held: no verified server-enforced output cap')
        raise CompletionHeld('server_output_cap_unverified')
    allowed_modes = {'chat_completions': {None,'chat_completions'},
                     'anthropic_messages': {None,'chat_completions','anthropic_messages'},
                     'gemini_native': {None,'chat_completions'},
                     'responses': {None,'codex_responses'}}
    if (route.api_mode not in allowed_modes.get(cap.transport,set())
            or route.runtime not in {None,'auto'}
            or (route.provider == 'anthropic' and not route.configured and route.base_url is not None)):
        raise CompletionHeld('transport_selection_unsupported')
    return cap


def _known_count(value):
    return value if type(value) is int and value >= 0 else None


async def _read(raw, limit=16384):
    data = bytearray()
    async for chunk in raw.iter_bytes():
        if len(data) + len(chunk) > limit:
            raise CompletionHeld('response_byte_envelope_exceeded_usage_unknown')
        data.extend(chunk)
    return json.loads(data)


def _install_response_limit(http):
    """Bound decoded bodies before SDK eager error parsing, not just success."""
    async def headers(response):
        if response.is_stream_consumed:
            if len(response.content) > 16384:
                await response.aclose()
                raise CompletionHeld('response_byte_envelope_exceeded_usage_unknown')
            return
        original = response.aiter_bytes
        async def bounded_bytes(chunk_size=None):
            size = 0
            try:
                async for chunk in original(chunk_size=chunk_size):
                    size += len(chunk)
                    if size > 16384:
                        raise CompletionHeld('response_byte_envelope_exceeded_usage_unknown')
                    yield chunk
            except BaseException:
                await response.aclose()
                raise
        response.aiter_bytes = bounded_bytes
    http.event_hooks['response'].insert(0, headers)


def call_deadline(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
        raise CompletionHeld('invalid_bounded_request')
    return asyncio.get_running_loop().time() + timeout


def remaining_budget(deadline):
    # Synchronous local reads cannot be preempted by asyncio. Always check
    # elapsed time after them, before physical dispatch and before publication.
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError('bounded completion deadline exceeded')
    return remaining


async def complete(*, config, messages, max_output_tokens, timeout, deadline=None) -> CompletionResult:
    own_deadline = call_deadline(timeout)
    deadline = own_deadline if deadline is None else min(deadline, own_deadline)
    # Deliver pending cancellation before any synchronous credential/config I/O.
    async with asyncio.timeout_at(deadline):
        await asyncio.sleep(0)
        result = await _complete(config=config, messages=messages,
                                 max_output_tokens=max_output_tokens, timeout=timeout, deadline=deadline)
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('bounded completion deadline exceeded')
        return result


async def _complete(*, config, messages, max_output_tokens, timeout, deadline) -> CompletionResult:
    from copy import deepcopy
    source_config = config
    config = deepcopy(config)
    messages = deepcopy(messages)
    if (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 512
            or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 5
            or not isinstance(messages, list) or not messages
            or len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode()) > 6144):
        raise CompletionHeld('invalid_bounded_request')
    if any(not isinstance(m,dict) or set(m) != {'role','content'}
           or m['role'] not in {'system','user','assistant'} or not isinstance(m['content'],str)
           for m in messages):
        raise CompletionHeld('invalid_text_message_roles')
    roles = [m['role'] for m in messages]
    if (any(role == 'system' and i != 0 for i,role in enumerate(roles))
            or any(left == right for left,right in zip(roles,roles[1:]))):
        raise CompletionHeld('invalid_role_sequence')
    route = _dispatch_route(config)
    cap = admitted(route)  # MUST precede auth, resolver, refresh or network.
    if asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError('bounded completion deadline exceeded')
    from agent import auxiliary_client as aux
    native = cap.transport != 'chat_completions'
    client, model = aux.resolve_provider_client(route.provider, model=route.model, async_mode=not native,
        raw_codex=cap.transport == 'responses', explicit_base_url=route.base_url,
        api_mode=route.api_mode or 'chat_completions', allow_fallback=False,
        bounded_config=config, bounded_source_config=source_config)
    # The offline resolver cannot perform network I/O. Yield/check the original
    # deadline before the first and only physical operation, even after slow I/O.
    try:
        await asyncio.sleep(0)
        authority = getattr(client, '_bounded_authority', None)
        if authority is not None:
            authority.validate()
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('bounded completion deadline exceeded')
    except BaseException:
        raw = getattr(client, '_real_client', client)
        close = getattr(raw, 'close', None)
        if close:
            result = close()
            if hasattr(result, '__await__'):
                await result
        raise
    if native:
        result = await _native_complete(client, model, route, cap, messages, max_output_tokens, timeout, deadline)
        if authority is not None:
            authority.validate()
        return replace(result, authority=authority)
    import openai
    if type(client) is not openai.AsyncOpenAI:
        raise CompletionHeld('native_async_transport_unavailable')
    try:
        if model != route.model:
            raise CompletionHeld('resolved_model_changed')
        from agent.bounded_provider_metadata import endpoint_identity
        if route.base_url is not None and endpoint_identity(str(client.base_url)) != endpoint_identity(aux._to_openai_base_url(route.base_url)):
            raise CompletionHeld('configured_endpoint_changed')
        actual = urlparse(str(client.base_url))
        if route.mode == 'strict_tokens' and (actual.scheme != 'https'
                or actual.hostname != 'api.openai.com' or actual.path.rstrip('/') != '/v1'):
            raise CompletionHeld('resolved_endpoint_cap_unverified')
        client._client.follow_redirects = False
        _install_response_limit(client._client)
        remaining = remaining_budget(deadline)
        bounded = client.with_options(max_retries=0, timeout=remaining)
        create = bounded.chat.completions.with_streaming_response.create
        async with asyncio.timeout_at(deadline):
            async with create(
                    model=model, messages=messages, stream=False, **{cap.output_limit_parameter: max_output_tokens}) as raw:
                body = await _read(raw)
        text = body['choices'][0]['message'].get('content') or ''
        usage = body.get('usage') or {}
        total = _known_count(usage.get('completion_tokens'))
        reasoning = _known_count((usage.get('completion_tokens_details') or {}).get('reasoning_tokens'))
        if route.mode == 'strict_tokens' and total is not None and total > max_output_tokens:
            raise CompletionHeld('provider_exceeded_output_contract_usage_reported')
        if authority is not None:
            authority.validate()
        return CompletionResult(text, route, cap, route.mode, 'known' if total is not None else 'unknown', total,
                                CompletionLimits(max_output_tokens,timeout,route.mode=='strict_tokens'), reasoning, authority)
    finally:
        await client.close()


async def _native_complete(client, model, route, cap, messages, tokens, timeout, deadline):
    """Reuse configured credential owners and native serializers, not thread I/O.

    Closing a connection cancels local transport and discards late results. It
    does not prove a remote service stopped generation or billing.
    """
    from agent import auxiliary_client as aux
    import httpx
    import openai
    from agent.gemini_native_adapter import GeminiNativeClient, build_gemini_request, bare_gemini_model_id
    raw = getattr(client, '_real_client', client)
    try:
        if model != route.model or client is None:
            raise CompletionHeld('configured_native_route_unavailable')
        base = str(client.base_url).rstrip('/')
        expected_base = route.base_url.rstrip('/') if route.base_url else None
        if cap.transport == 'gemini_native' and expected_base and expected_base.endswith('/openai'):
            expected_base = expected_base[:-len('/openai')]
        from agent.bounded_provider_metadata import endpoint_identity
        if expected_base is not None and endpoint_identity(base) != endpoint_identity(expected_base):
            raise CompletionHeld('configured_endpoint_changed')
        actual = urlparse(base)
        strict_endpoint = ((actual.hostname == 'api.anthropic.com' and actual.path.rstrip('/') in {'','/v1'})
                           if cap.transport == 'anthropic_messages' else
                           (actual.hostname == 'api.openai.com' and actual.path.rstrip('/') == '/v1'))
        if route.mode == 'strict_tokens' and (actual.scheme != 'https' or not strict_endpoint):
            raise CompletionHeld('resolved_endpoint_cap_unverified')
        if cap.transport == 'anthropic_messages' and isinstance(client, aux.AnthropicAuxiliaryClient):
            from agent.anthropic_adapter import build_anthropic_kwargs
            payload = build_anthropic_kwargs(model=model, messages=messages, tools=None,
                max_tokens=tokens, reasoning_config=None, is_oauth=client.chat.completions._is_oauth, base_url=base)
            if payload.get('max_tokens') != tokens:
                raise CompletionHeld('native_serializer_exceeds_output_envelope')
            headers = dict(raw.default_headers)
            url = base.rstrip('/') + ('/messages' if base.endswith('/v1') else '/v1/messages')
        elif cap.transport == 'gemini_native' and isinstance(client, GeminiNativeClient):
            payload = build_gemini_request(model=model,messages=messages,max_tokens=tokens)
            headers = client._headers()
            url = base+'/models/'+bare_gemini_model_id(model)+':generateContent'
        elif cap.transport == 'responses' and type(client) is openai.OpenAI:
            from agent.codex_responses_adapter import _chat_messages_to_responses_input
            systems = [m['content'] for m in messages if m['role']=='system']
            if len(systems)>1:
                raise CompletionHeld('multiple_response_system_messages_unsupported')
            payload = {'model':aux._strip_codex_ctx_variant(model),'store':False,'stream':True,
                'instructions':systems[0] if systems else '',
                'input':_chat_messages_to_responses_input([m for m in messages if m['role']!='system'],
                    native_compaction_eligible=False)}
            if cap.output_limit_parameter is not None:
                payload[cap.output_limit_parameter] = tokens
            headers = dict(raw.default_headers)
            url = base+'/responses'
        else:
            raise CompletionHeld('native_transport_type_unverified')
        headers.update(getattr(client, '_bounded_headers', {}))
        if getattr(client, '_bounded_keyless', False):
            # The established public-route policy is anonymous on every native
            # wire; never serialize its SDK-construction placeholder as auth.
            headers = {k:v for k,v in headers.items()
                       if k.lower() not in {'authorization', 'x-api-key', 'x-goog-api-key'}}
        # Fresh native async HTTP, preserving shared TLS/proxy configuration.
        http = aux._openai_http_client_kwargs(base,async_mode=True).get('http_client')
        if not isinstance(http,httpx.AsyncClient):
            raise CompletionHeld('native_async_http_unavailable')
        http.follow_redirects = False
        headers = {k:v for k,v in headers.items() if isinstance(v,str)}
        headers['content-type'] = 'application/json'
        data = bytearray()
        async with http:
            remaining = remaining_budget(deadline)
            async with asyncio.timeout_at(deadline):
                async with http.stream('POST',url,json=payload,headers=headers,timeout=remaining) as response:
                    # No redirects, retry, credential refresh or recovery here.
                    if response.status_code != 200:
                        raise CompletionHeld('provider_http_'+str(response.status_code)+'_usage_unknown')
                    async for chunk in response.aiter_bytes():
                        if len(data)+len(chunk)>16384:
                            raise CompletionHeld('response_byte_envelope_exceeded_usage_unknown')
                        data.extend(chunk)
        if cap.transport == 'responses':
            completed = None
            for line in data.decode().splitlines():
                if not line.startswith('data: ') or line[6:] == '[DONE]':
                    continue
                event = json.loads(line[6:])
                if event.get('type') == 'response.completed':
                    completed = event.get('response')
            if not isinstance(completed,dict):
                raise CompletionHeld('incomplete_response_usage_unknown')
            text = ''.join(part.get('text','') for item in completed.get('output',[])
                           if item.get('type')=='message' for part in item.get('content',[])
                           if part.get('type')=='output_text')
            usage = completed.get('usage') or {}
            total = _known_count(usage.get('output_tokens'))
            reasoning = _known_count((usage.get('output_tokens_details') or {}).get('reasoning_tokens'))
        elif cap.transport == 'anthropic_messages':
            body = json.loads(data)
            text = ''.join(part.get('text','') for part in body.get('content',[]) if part.get('type')=='text')
            total = _known_count((body.get('usage') or {}).get('output_tokens'))
            reasoning = None
        else:
            body = json.loads(data)
            text = ''.join(part.get('text','') for item in body.get('candidates',[])
                           for part in item.get('content',{}).get('parts',[]) if not part.get('thought'))
            usage = body.get('usageMetadata') or {}
            visible = _known_count(usage.get('candidatesTokenCount'))
            reasoning = _known_count(usage.get('thoughtsTokenCount'))
            # Candidate count alone is NOT total output when thoughts are unknown.
            total = visible+reasoning if visible is not None and reasoning is not None else None
        if route.mode == 'strict_tokens' and total is not None and total > tokens:
            raise CompletionHeld('provider_exceeded_output_contract_usage_reported')
        return CompletionResult(text,route,cap,route.mode,'known' if total is not None else 'unknown',total,
                                CompletionLimits(tokens,timeout,route.mode=='strict_tokens'),reasoning)
    finally:
        close = getattr(raw,'close',None)
        if callable(close):
            close()
