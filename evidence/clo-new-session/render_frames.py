import random
from agent import clover_flavor
from clover_cli import skin_engine
from plugins.platforms.telegram.adapter import TelegramAdapter

skin_engine.set_active_skin("clover")
clover_flavor.reset_new_session_picks()
frames = []
for i in range(5):
    headline, info, tip = clover_flavor.render_new_session(
        chat_key="frame-demo", model="grok-4.7", provider="xai-oauth",
        context="500K", tip="Try /compress when chats get long.",
        rng=random.Random(i + 2),
    )
    frames.append(TelegramAdapter.format_message(None, "\n\n".join(x for x in (headline, info, tip) if x)))
headline, info, tip = clover_flavor.render_new_session(
    chat_key="frame-title", title="my_proj v2", model="grok-4.7", provider="xai-oauth",
    context="500K", tip="Try /compress when chats get long.", rng=random.Random(12),
)
frames.append(TelegramAdapter.format_message(None, "\n\n".join(x for x in (headline, info, tip) if x)))
skin_engine.set_active_skin("default")
classic = "✨ Session reset! Starting fresh.\n\n◆ Model: `grok-4.7`\n◆ Provider: xai-oauth\n◆ Context: 500K tokens (detected)\n✦ Tip: Try /compress when chats get long."
frames.append(TelegramAdapter.format_message(None, classic))
with open("evidence/clo-new-session/frames.txt", "w", encoding="utf-8") as f:
    f.write("\n\n--- FRAME ---\n\n".join(frames) + "\n")
print("Rendered", len(frames), "Telegram frames")
