# Telegram Voice Capability Preflight for TTS Delivery

## Summary

Add a Telegram Bot API preflight check before sending synthesized speech as a native Telegram voice message.

When a private chat has restricted voice and video note delivery, the integration must skip `sendVoice` and immediately use a non-voice fallback delivery method.

## Background

Telegram Bot API exposes this capability through `getChat` → `ChatFullInfo`:

- Method: <https://core.telegram.org/bots/api#getchat>
- Object: <https://core.telegram.org/bots/api#chatfullinfo>
- Field: `has_restricted_voice_and_video_messages`

Telegram's documented meaning:

> True, if the privacy settings of the other party restrict sending voice and video note messages in the private chat.

## Scope

Apply this check only to Telegram private chats:

```text
ChatFullInfo.type == "private"
```

Do not use this field as a per-user capability check for groups, supergroups, or channels. In those chats, `sendVoice` targets the chat itself, not a specific recipient's private settings.

## Required behavior

Before sending `.ogg` / `.opus` TTS output through `sendVoice` in a Telegram private chat:

1. Call `getChat(chat_id)`.
2. Inspect `ChatFullInfo.has_restricted_voice_and_video_messages`.
3. If the value is `true`, do not call `sendVoice`; use fallback delivery immediately.
4. If the value is `false` or the field is absent, `sendVoice` may be used.
5. If `getChat` fails, fall back to the existing runtime behavior: try `sendVoice` and handle Telegram errors.

Pseudo-code:

```python
chat = await bot.get_chat(chat_id)

if chat.type == "private" and getattr(chat, "has_restricted_voice_and_video_messages", False) is True:
    await send_tts_fallback(...)
else:
    await send_voice(...)
```

## Field interpretation

| API result | Meaning | Action |
|---|---|---|
| `has_restricted_voice_and_video_messages == true` | The private chat restricts voice/video note delivery | Skip `sendVoice`; use fallback |
| `has_restricted_voice_and_video_messages == false` | No restriction reported | Use `sendVoice` |
| Field absent / `None` | No restriction reported by Telegram | Use `sendVoice` |
| `getChat` error | Preflight inconclusive | Try `sendVoice`; keep runtime fallback |

## Runtime fallback remains mandatory

The preflight check does not replace runtime error handling.

The implementation must still catch Telegram errors such as:

```text
VOICE_MESSAGES_FORBIDDEN
```

On this error:

1. Mark voice delivery as restricted for the chat.
2. Send the same TTS output through fallback delivery.
3. Avoid losing or dropping the assistant response.

Reasons to keep runtime fallback:

- The user can change privacy settings between `getChat` and `sendVoice`.
- A Bot API implementation or client library may not expose the new field.
- Network/API errors can make preflight inconclusive.
- Race conditions are possible.

## Capability cache

Cache the capability result by Telegram `chat_id`.

Suggested key:

```text
telegram:voice_capability:<chat_id>
```

Suggested value:

```json
{
  "restricted": true,
  "checked_at": "<iso timestamp>",
  "source": "getChat | sendVoice_error | sendVoice_success"
}
```

Rules:

1. If `getChat` returns `has_restricted_voice_and_video_messages == true`, store `restricted = true`.
2. If `getChat` returns `false` or omits the field, store `restricted = false`.
3. If `sendVoice` succeeds, store `restricted = false`.
4. If `sendVoice` fails with `VOICE_MESSAGES_FORBIDDEN`, store `restricted = true`.
5. Use a TTL because the user can change the setting later. A TTL between 1 and 24 hours is acceptable.

## Fallback delivery

When voice delivery is restricted, deliver the same TTS output through a non-voice method, for example:

1. `sendAudio`, if the format is supported by Telegram.
2. `sendDocument`, if the audio format is not accepted by `sendAudio`.
3. Text-only response as the last resort if media delivery fails.

Fallback delivery must not require the user to repeat the request.

## Acceptance criteria

- `getChat` is called before `sendVoice` for Telegram private-chat TTS voice delivery.
- `has_restricted_voice_and_video_messages == true` prevents calling `sendVoice`.
- `false` or absent field allows `sendVoice`.
- `getChat` failure does not drop the response.
- `VOICE_MESSAGES_FORBIDDEN` is still handled as a runtime fallback path.
- Capability result is cached by Telegram `chat_id` with a TTL.
- Unit tests cover:
  - field is `true`;
  - field is `false`;
  - field is absent;
  - `getChat` failure;
  - `sendVoice` raises `VOICE_MESSAGES_FORBIDDEN`;
  - group/supergroup chats do not apply private-user preflight logic.

## Privacy note

This specification intentionally contains no personal chat IDs, usernames, user names, tokens, or live account details.
