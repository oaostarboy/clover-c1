## Summary
- Give Clo and message-pack skins with `new_session` lines a pooled, per-chat `/new` and `/reset` headline, quote-bar model/context/local endpoint info, and italic platform-appropriate tip.
- Preserve stock replies for other skins and languages; retain topic headers, title warnings, and ephemeral delivery.
- Use 🤖 for the model label because 🧠 already means memory in the Clover skin/tool map.
- Telegram formatting frames (five random draws, titled, classic): [`evidence/clo-new-session/frames.txt`](evidence/clo-new-session/frames.txt).

## Sample Telegram renders
```
☘️(◕ᴗ◕✿) fresh patch of clover. what's next?

> 🤖 Grok 4.7 · xAI
> 📏 500K context

🍀 tip: _Try /compress when chats get long._
```
```
☘️(ﾉ◕ヮ◕)ﾉ new patch: _my\_proj v2_

> 🤖 Grok 4.7 · xAI
> 📏 500K context

🍀 tip: _Try /compress when chats get long._
```
```
✨ Session reset\! Starting fresh\.

◆ Model: `grok-4.7`
◆ Provider: xai\-oauth
◆ Context: 500K tokens \(detected\)
✦ Tip: Try /compress when chats get long\.
```

## Tests
`tests/gateway/test_clo_new_session.py tests/gateway/test_clo_polish.py tests/gateway/test_clover_acks.py tests/agent/test_clover_skin.py tests/gateway/test_discord_slash_commands.py` — 230 passed. Four warnings are existing AsyncMock coroutine warnings during title-path test setup.