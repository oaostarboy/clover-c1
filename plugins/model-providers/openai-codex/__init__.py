"""OpenAI Codex (Responses API) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

openai_codex = ProviderProfile(
    name="openai-codex",
    aliases=("codex", "openai_codex"),
    api_mode="codex_responses",
    env_vars=(),  # OAuth external — no API key
    base_url="https://chatgpt.com/backend-api/codex",
    auth_type="oauth_external",
    # GPT-6 Luna: OpenAI's small fast tier. Probed live on the Codex OAuth
    # backend (p50 ~0.85 s vs ~3.9 s for gpt-6.1-sol on the router prompt);
    # the "-mini" ids are rejected there ("not supported when using Codex with
    # a ChatGPT account").
    fast_aux_model="gpt-6-luna",
)

register_provider(openai_codex)
