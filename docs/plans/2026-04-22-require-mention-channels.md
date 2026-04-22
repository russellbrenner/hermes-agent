# Cross-Platform `require_mention_channels` Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add `require_mention_channels` config key across all 7 gateway platforms that have mention-gating, enabling per-channel mention overrides when `require_mention=false`. Also fix Mattermost and Matrix to use the `config.extra` pattern instead of raw `os.getenv()`.

**Origin:** Community request from neeldhara on PR #3664 — they want most channels to respond freely but a few "agent group" channels to require @mention.

**Architecture:** Each platform gets a new `_<platform>_require_mention_channels()` helper that returns a set of channel/chat/room IDs. The mention gate logic adds one new check: if `require_mention` is false but the channel is in `require_mention_channels`, treat it as requiring mention for that channel. Mattermost and Matrix are additionally refactored to use helper methods matching the Discord/Slack/Telegram pattern.

**Priority order of logic (highest wins):**
1. DMs → always respond
2. Channel in `free_response_channels` → never require mention
3. Channel in `require_mention_channels` → always require mention
4. Global `require_mention` setting → fallback

---

## Phase 0: Mattermost + Matrix config.extra alignment

Before adding the new feature, bring Mattermost and Matrix up to par with the other platforms by extracting their inline `os.getenv()` calls into proper helper methods that check `config.extra` first.

### Task 0A: Extract Mattermost mention helpers

**Objective:** Replace inline `os.getenv()` in Mattermost's `_handle_ws_event` with proper helper methods matching the Discord/Slack pattern.

**Files:**
- Modify: `gateway/platforms/mattermost.py` (lines 619-626)

**Implementation:**

Add three helper methods to `MattermostAdapter`:

```python
def _mattermost_require_mention(self) -> bool:
    """Return whether Mattermost channel messages require a bot mention."""
    configured = self.config.extra.get("require_mention")
    if configured is not None:
        if isinstance(configured, str):
            return configured.lower() not in ("false", "0", "no", "off")
        return bool(configured)
    return os.getenv("MATTERMOST_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no", "off")

def _mattermost_free_response_channels(self) -> set:
    """Return Mattermost channel IDs where no bot mention is required."""
    raw = self.config.extra.get("free_response_channels")
    if raw is None:
        raw = os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

Then replace the inline code in `_handle_ws_event` (lines 620-626):
```python
# Before:
require_mention = os.getenv("MATTERMOST_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no")
free_channels_raw = os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "")
free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
is_free_channel = channel_id in free_channels

# After:
require_mention = self._mattermost_require_mention()
free_channels = self._mattermost_free_response_channels()
is_free_channel = channel_id in free_channels
```

**Tests:**
- Modify: `tests/gateway/test_mattermost.py`
- Add unit tests for `_mattermost_require_mention()` and `_mattermost_free_response_channels()` that test both `config.extra` and env var fallback, matching the pattern in `tests/gateway/test_slack_mention.py`.
- Update existing tests in `TestMattermostMentionBehavior` to also test `config.extra` path.

### Task 0B: Extract Matrix mention helpers

**Objective:** Replace inline `os.getenv()` in Matrix's `__init__` with proper helper methods.

**Files:**
- Modify: `gateway/platforms/matrix.py`

**Implementation:**

Matrix currently reads mention config in `__init__` and stores as instance vars:
```python
self._require_mention = os.getenv("MATRIX_REQUIRE_MENTION", "true").lower() in ("true", "1", "yes")
self._free_rooms = ...
```

Refactor to use helper methods (called from the gate logic, not __init__):
```python
def _matrix_require_mention(self) -> bool:
    """Return whether Matrix room messages require a bot mention."""
    configured = self.config.extra.get("require_mention")
    if configured is not None:
        if isinstance(configured, str):
            return configured.lower() not in ("false", "0", "no", "off")
        return bool(configured)
    return os.getenv("MATRIX_REQUIRE_MENTION", "true").lower() not in ("false", "0", "no", "off")

def _matrix_free_response_rooms(self) -> set:
    """Return Matrix room IDs where no bot mention is required."""
    raw = self.config.extra.get("free_response_rooms")
    if raw is None:
        raw = os.getenv("MATRIX_FREE_RESPONSE_ROOMS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

Then replace `self._require_mention` and `self._free_rooms` usage in the gate logic with calls to these methods.

**Note:** Keep the `__init__` assignments for backward compat if anything else references `self._require_mention` — search the codebase first. If nothing else reads them, remove them entirely.

**Tests:**
- Modify: `tests/gateway/test_matrix_mention.py`
- Add unit tests for the new helper methods.
- Update existing tests to verify config.extra path.

---

## Phase 1: Add `require_mention_channels` to all 7 platforms

Each platform needs: (1) a new helper method, (2) updated gate logic, (3) tests, (4) docs.

### Task 1A: Discord `require_mention_channels`

**Objective:** Add `_discord_require_mention_channels()` helper and update gate logic.

**Files:**
- Modify: `gateway/platforms/discord.py`
  - Add `_discord_require_mention_channels()` method (near line 2489, after `_discord_free_response_channels`)
  - Update gate logic at line 3010

**Helper method:**
```python
def _discord_require_mention_channels(self) -> set:
    """Return Discord channel IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_channels")
    if raw is None:
        raw = os.getenv("DISCORD_REQUIRE_MENTION_CHANNELS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change** (discord.py around line 2994-3012):
```python
# Current:
if require_mention and not is_free_channel and not in_bot_thread:
    if self._client.user not in message.mentions and not mention_prefix:
        return

# New:
require_mention_chs = self._discord_require_mention_channels()
is_force_mention_channel = bool(channel_ids & require_mention_chs)

if is_free_channel:
    pass  # Free-response always wins
elif is_force_mention_channel or require_mention:
    if not in_bot_thread:
        if self._client.user not in message.mentions and not mention_prefix:
            return
```

**Tests:**
- Modify: `tests/gateway/test_discord_free_response.py`
- Add:
  - `test_discord_require_mention_channels_forces_mention_when_global_disabled`
  - `test_discord_require_mention_channels_from_config_extra`
  - `test_discord_require_mention_channels_from_env_var`
  - `test_discord_free_response_overrides_require_mention_channels`

### Task 1B: Slack `require_mention_channels`

**Objective:** Add `_slack_require_mention_channels()` and update gate logic.

**Files:**
- Modify: `gateway/platforms/slack.py` (near line 1685, after `_slack_free_response_channels`)
- Modify gate logic at line 1043-1065

**Helper method:**
```python
def _slack_require_mention_channels(self) -> set:
    """Return Slack channel IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_channels")
    if raw is None:
        raw = os.getenv("SLACK_REQUIRE_MENTION_CHANNELS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change** (slack.py around line 1043-1065):
```python
# Current:
if not is_dm and bot_uid:
    if channel_id in self._slack_free_response_channels():
        pass  # Free-response channel
    elif not self._slack_require_mention():
        pass  # Mention requirement disabled
    elif not is_mentioned:
        ...check threads...
        if not any_thread_bypass:
            return

# New:
if not is_dm and bot_uid:
    if channel_id in self._slack_free_response_channels():
        pass  # Free-response channel — always process
    elif channel_id in self._slack_require_mention_channels():
        # Force-mention channel — require mention even if global is off
        if not is_mentioned:
            ...check threads...
            if not any_thread_bypass:
                return
    elif not self._slack_require_mention():
        pass  # Mention requirement disabled globally
    elif not is_mentioned:
        ...check threads...
        if not any_thread_bypass:
            return
```

**Tests:**
- Modify: `tests/gateway/test_slack_mention.py`
- Add:
  - `test_require_mention_channels_forces_mention_when_global_disabled`
  - `test_require_mention_channels_from_config_extra`
  - `test_require_mention_channels_from_env_var`
  - `test_free_response_overrides_require_mention_channels`

### Task 1C: Telegram `require_mention_channels`

**Objective:** Add `_telegram_require_mention_chats()` and update `_should_process_message`.

**Files:**
- Modify: `gateway/platforms/telegram.py`

**Helper method:**
```python
def _telegram_require_mention_chats(self) -> set[str]:
    """Return Telegram chat IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_chats")
    if raw is None:
        raw = os.getenv("TELEGRAM_REQUIRE_MENTION_CHATS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change** in `_should_process_message`:
```python
# Current order:
#   free_response_chats -> bypass
#   require_mention=False -> bypass
#   reply-to-bot, mentions, patterns -> accept

# New order:
#   free_response_chats -> bypass
#   require_mention_chats -> force mention (even when global=False)
#   require_mention=False -> bypass
#   reply-to-bot, mentions, patterns -> accept
```

Insert after the `free_response_chats` check and before the `require_mention` check:
```python
if str(chat.id) in self._telegram_require_mention_chats():
    # Force mention required in this chat even when global require_mention is off
    if self._is_reply_to_bot(message):
        return True
    if self._message_mentions_bot(message):
        return True
    return self._message_matches_mention_patterns(message)
```

**Tests:**
- Modify: `tests/gateway/test_telegram_group_gating.py`
- Add:
  - `test_require_mention_chats_forces_mention_when_global_disabled`
  - `test_free_response_overrides_require_mention_chats`

### Task 1D: WhatsApp `require_mention_channels`

**Objective:** Add `_whatsapp_require_mention_chats()` and update `_should_process_message`.

**Files:**
- Modify: `gateway/platforms/whatsapp.py`

**Helper method:**
```python
def _whatsapp_require_mention_chats(self) -> set[str]:
    """Return WhatsApp chat IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_chats")
    if raw is None:
        raw = os.getenv("WHATSAPP_REQUIRE_MENTION_CHATS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change** — same pattern as Telegram: insert force-mention check after `free_response_chats` and before the global `require_mention` check.

**Tests:**
- Modify: `tests/gateway/test_whatsapp_group_gating.py`

### Task 1E: DingTalk `require_mention_channels`

**Objective:** Add `_dingtalk_require_mention_chats()` and update `_should_process_message`.

**Files:**
- Modify: `gateway/platforms/dingtalk.py`

**Helper method:** Same pattern as WhatsApp/Telegram.

**Gate logic change:** Same insertion point — after `free_response_chats`, before global `require_mention`.

**Tests:**
- Modify: `tests/gateway/test_dingtalk.py`

### Task 1F: Mattermost `require_mention_channels`

**Objective:** Add `_mattermost_require_mention_channels()` and update gate logic (uses helpers from Task 0A).

**Files:**
- Modify: `gateway/platforms/mattermost.py`

**Helper method:**
```python
def _mattermost_require_mention_channels(self) -> set:
    """Return Mattermost channel IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_channels")
    if raw is None:
        raw = os.getenv("MATTERMOST_REQUIRE_MENTION_CHANNELS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change** in `_handle_ws_event`:
```python
# After Task 0A, the logic already uses helper methods. Now add:
require_mention_chs = self._mattermost_require_mention_channels()
is_force_mention = channel_id in require_mention_chs

if is_free_channel:
    pass  # Free-response always wins
elif is_force_mention or require_mention:
    if not has_mention:
        logger.debug(...)
        return
```

**Tests:**
- Modify: `tests/gateway/test_mattermost.py`
- Add:
  - `test_require_mention_channels_forces_mention_when_global_disabled`
  - `test_require_mention_channels_from_config_extra`
  - `test_require_mention_channels_from_env_var`
  - `test_free_response_overrides_require_mention_channels`

### Task 1G: Matrix `require_mention_channels`

**Objective:** Add `_matrix_require_mention_rooms()` and update gate logic (uses helpers from Task 0B).

**Files:**
- Modify: `gateway/platforms/matrix.py`

**Helper method:**
```python
def _matrix_require_mention_rooms(self) -> set:
    """Return Matrix room IDs where bot mention is always required."""
    raw = self.config.extra.get("require_mention_rooms")
    if raw is None:
        raw = os.getenv("MATRIX_REQUIRE_MENTION_ROOMS", "")
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, str) and raw.strip():
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set()
```

**Gate logic change:**
```python
if not is_dm:
    is_free_room = room_id in self._matrix_free_response_rooms()
    is_force_mention = room_id in self._matrix_require_mention_rooms()
    in_bot_thread = bool(thread_id and thread_id in self._threads)
    if is_free_room:
        pass  # Free room — always respond
    elif (is_force_mention or self._matrix_require_mention()) and not in_bot_thread:
        if not is_mentioned:
            return None
```

**Tests:**
- Modify: `tests/gateway/test_matrix_mention.py`

---

## Phase 2: Config bridging in `gateway/config.py`

### Task 2-pre: Add config.yaml → env var bridging for `require_mention_channels`

**Objective:** Bridge the new config.yaml keys to env vars in `gateway/config.py`'s `load_gateway_config()`, following the exact pattern used for `free_response_channels`.

**Files:**
- Modify: `gateway/config.py`

**Implementation:** For EACH platform section, add the bridging block right after the existing `free_response_channels` bridge:

```python
# Slack (after line 618):
rmc = slack_cfg.get("require_mention_channels")
if rmc is not None and not os.getenv("SLACK_REQUIRE_MENTION_CHANNELS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["SLACK_REQUIRE_MENTION_CHANNELS"] = str(rmc)

# Discord (after line 629):
rmc = discord_cfg.get("require_mention_channels")
if rmc is not None and not os.getenv("DISCORD_REQUIRE_MENTION_CHANNELS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["DISCORD_REQUIRE_MENTION_CHANNELS"] = str(rmc)

# Telegram (after line 678):
rmc = telegram_cfg.get("require_mention_chats")
if rmc is not None and not os.getenv("TELEGRAM_REQUIRE_MENTION_CHATS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["TELEGRAM_REQUIRE_MENTION_CHATS"] = str(rmc)

# WhatsApp (after line 709):
rmc = whatsapp_cfg.get("require_mention_chats")
if rmc is not None and not os.getenv("WHATSAPP_REQUIRE_MENTION_CHATS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["WHATSAPP_REQUIRE_MENTION_CHATS"] = str(rmc)

# DingTalk (after line 736):
rmc = dingtalk_cfg.get("require_mention_chats")
if rmc is not None and not os.getenv("DINGTALK_REQUIRE_MENTION_CHATS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["DINGTALK_REQUIRE_MENTION_CHATS"] = str(rmc)

# Mattermost (new section — mattermost currently has NO bridging at all):
mattermost_cfg = yaml_cfg.get("mattermost", {})
if isinstance(mattermost_cfg, dict):
    if "require_mention" in mattermost_cfg and not os.getenv("MATTERMOST_REQUIRE_MENTION"):
        os.environ["MATTERMOST_REQUIRE_MENTION"] = str(mattermost_cfg["require_mention"]).lower()
    frc = mattermost_cfg.get("free_response_channels")
    if frc is not None and not os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["MATTERMOST_FREE_RESPONSE_CHANNELS"] = str(frc)
    rmc = mattermost_cfg.get("require_mention_channels")
    if rmc is not None and not os.getenv("MATTERMOST_REQUIRE_MENTION_CHANNELS"):
        if isinstance(rmc, list):
            rmc = ",".join(str(v) for v in rmc)
        os.environ["MATTERMOST_REQUIRE_MENTION_CHANNELS"] = str(rmc)

# Matrix (after line 752):
rmc = matrix_cfg.get("require_mention_rooms")
if rmc is not None and not os.getenv("MATRIX_REQUIRE_MENTION_ROOMS"):
    if isinstance(rmc, list):
        rmc = ",".join(str(v) for v in rmc)
    os.environ["MATRIX_REQUIRE_MENTION_ROOMS"] = str(rmc)
```

**Note:** Mattermost currently has ZERO config bridging in `gateway/config.py` — this is an existing gap. We add a full bridging block for Mattermost while we're here (require_mention, free_response_channels, AND the new require_mention_channels).

---

## Phase 3: Config, env vars, and docs

### Task 3A: Add env vars to `hermes_cli/config.py`

**Objective:** Register the new env vars in `OPTIONAL_ENV_VARS` for platforms that already have entries there (Mattermost, Matrix). Add DEFAULT_CONFIG entries where appropriate.

**Files:**
- Modify: `hermes_cli/config.py`

**Add to OPTIONAL_ENV_VARS:**
```python
"MATTERMOST_REQUIRE_MENTION_CHANNELS": {
    "description": "Comma-separated Mattermost channel IDs where bot always requires @mention (even when require_mention is false)",
    "prompt": "Require-mention channel IDs (comma-separated)",
    "url": None,
    "password": False,
    "category": "messaging",
},
"MATRIX_REQUIRE_MENTION_ROOMS": {
    "description": "Comma-separated Matrix room IDs where bot always requires @mention (even when require_mention is false)",
    "prompt": "Require-mention room IDs (comma-separated)",
    "url": None,
    "password": False,
    "category": "messaging",
    "advanced": True,
},
```

**Add to DEFAULT_CONFIG discord section (if it already has require_mention/free_response_channels):**
```python
"discord": {
    "require_mention": True,
    "free_response_channels": "",
    "require_mention_channels": "",  # <-- new
}
```

### Task 3B: Update docs — Mattermost

**Objective:** Update Mattermost docs with new config key and env var.

**Files:**
- Modify: `website/docs/user-guide/messaging/mattermost.md`

**Changes:**
1. Add `MATTERMOST_REQUIRE_MENTION_CHANNELS` to the Mention Behavior table
2. Add config.yaml example showing the new key
3. Add a use case example matching neeldhara's scenario

### Task 3C: Update docs — Discord

**Files:**
- Modify: `website/docs/user-guide/messaging/discord.md`

Add `require_mention_channels` to the config.yaml example and env var table.

### Task 3D: Update docs — Slack

**Files:**
- Modify: `website/docs/user-guide/messaging/slack.md`

### Task 3E: Update docs — Telegram

**Files:**
- Modify: `website/docs/user-guide/messaging/telegram.md`

### Task 3F: Update docs — Matrix

**Files:**
- Modify: `website/docs/user-guide/messaging/matrix.md`

### Task 3G: Update docs — environment-variables.md

**Files:**
- Modify: `website/docs/reference/environment-variables.md`

Add entries for all new env vars:
- `DISCORD_REQUIRE_MENTION_CHANNELS`
- `SLACK_REQUIRE_MENTION_CHANNELS`
- `TELEGRAM_REQUIRE_MENTION_CHATS`
- `WHATSAPP_REQUIRE_MENTION_CHATS`
- `DINGTALK_REQUIRE_MENTION_CHATS`
- `MATTERMOST_REQUIRE_MENTION_CHANNELS`
- `MATRIX_REQUIRE_MENTION_ROOMS`

### Task 3H: Update docs — configuration.md

**Files:**
- Modify: `website/docs/user-guide/configuration.md`

Add `require_mention_channels` to the generic config example near lines 1234-1240.

---

## Phase 4: Config bridging tests

### Task 4A: Config bridging tests for platforms that have them

**Objective:** Ensure config.yaml `require_mention_channels` key is bridged to the env var correctly for platforms that use the config bridging pattern.

**Files:**
- Modify tests that already have `test_config_bridges_*` functions

For platforms using config.yaml bridging (Telegram, WhatsApp, Slack, DingTalk):
```python
def test_config_bridges_require_mention_channels(monkeypatch, tmp_path):
    """config.yaml require_mention_chats bridges to env var."""
    # Write config with require_mention_chats list
    # Load config
    # Assert the env var is set correctly
```

---

## Naming Convention Summary

Each platform uses its own terminology for channels/chats/rooms. The new key follows the same convention:

| Platform   | free_response key            | NEW require_mention key             | Env var                              |
|------------|------------------------------|-------------------------------------|--------------------------------------|
| Discord    | `free_response_channels`     | `require_mention_channels`          | `DISCORD_REQUIRE_MENTION_CHANNELS`   |
| Slack      | `free_response_channels`     | `require_mention_channels`          | `SLACK_REQUIRE_MENTION_CHANNELS`     |
| Telegram   | `free_response_chats`        | `require_mention_chats`             | `TELEGRAM_REQUIRE_MENTION_CHATS`     |
| WhatsApp   | `free_response_chats`        | `require_mention_chats`             | `WHATSAPP_REQUIRE_MENTION_CHATS`     |
| DingTalk   | `free_response_chats`        | `require_mention_chats`             | `DINGTALK_REQUIRE_MENTION_CHATS`     |
| Mattermost | `free_response_channels`     | `require_mention_channels`          | `MATTERMOST_REQUIRE_MENTION_CHANNELS`|
| Matrix     | `free_response_rooms`        | `require_mention_rooms`             | `MATRIX_REQUIRE_MENTION_ROOMS`       |

---

## Priority Logic (all platforms, documented for consistency)

```
1. Is DM? → ALWAYS respond (no mention check)
2. Channel in free_response_*? → RESPOND (no mention needed)
3. Channel in require_mention_*? → REQUIRE MENTION (even if global require_mention=false)
4. Global require_mention=true? → REQUIRE MENTION
5. Global require_mention=false? → RESPOND (no mention needed)
```

`free_response` takes priority over `require_mention_channels` because explicitly marking a channel as "free response" is a stronger signal than the channel appearing in both lists (which would be a config error, but we fail-open for free_response).

---

## Verification

After all tasks, run:
```bash
bash scripts/run_tests.sh tests/gateway/
```

All existing tests must pass (backward compat), plus the new tests for `require_mention_channels`.
