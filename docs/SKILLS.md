# Using the skill library

The skills under `skills/` are standard Agent Skills: markdown files
with YAML frontmatter (`name`, `description`) plus the instructions
Claude / any LLM should follow when the skill applies.

How a given MCP client picks these up varies:

## Claude Code

Native auto-discovery. When you install this repo as a plugin (either
via the marketplace or `claude --plugin-dir .`), every skill under
`skills/` is loaded automatically. The model decides on its own when
to invoke one based on the `description` frontmatter.

## Claude Desktop

Claude Desktop reads skills from `~/Library/Application Support/
Claude/skills/`. Symlink or copy the ones you want:

```bash
ln -s "$(pwd)/skills/computer-use" \
      "$HOME/Library/Application Support/Claude/skills/computer-use"
ln -s "$(pwd)/skills/safari" \
      "$HOME/Library/Application Support/Claude/skills/safari"
# …and so on for each skill you want available
```

Restart Claude Desktop. Skills appear in the `/skills` picker.

## Cursor

Cursor uses its own rules system (`.cursor/rules/`), not Agent Skills.
Two options:

1. Point the Cursor-native rules runtime at the skills via a
   frontmatter bridge: add a `.cursor/rules/background-computer-use.mdc`
   that `@import`s `../skills/computer-use/SKILL.md`.
2. Or just tell Cursor in your system prompt: "When interacting with
   macOS apps via the `background-computer-use` MCP server, read the
   instructions at `skills/computer-use/SKILL.md`." Cursor's agent
   will `Read` the file before its first tool call.

## Codex CLI

Codex does not auto-load Agent Skills today. Cat the top-level skill
into the system prompt when you want it active:

```bash
codex -s "$(cat skills/computer-use/SKILL.md)"
```

Or use `codex-config`'s `instructions` field.

## Goose / Zed / Continue / any other MCP client

If the client doesn't support Agent Skills natively, you can either:

- Inline the relevant `SKILL.md` content into your system prompt when
  starting the session.
- Prompt the model once: "Please read
  `skills/computer-use/SKILL.md` before using the
  `background-computer-use` MCP tools." Most capable models will use
  their file-read tool automatically.

## What's in each skill

| Skill | When it applies |
|-------|-----------------|
| `computer-use` | Every macOS GUI automation request. Teaches the observe → decide → act → re-observe loop. |
| `safari` | Safari-specific quirks (URL bar sticky value, WKWebView scroll routing, tab manipulation). |
| `chrome` | Chromium-specific quirks (`AXManualAccessibility` auto-opt-in, AppleScript JS scroll, AX tree depth). |
| `clock` | Timer / alarm / world-clock interactions in Clock.app. |
| `numbers` | Spreadsheet cell editing patterns (single-click vs triple-click, formula entry). |
| `apple-music` | Playback, queue, and library navigation. |
| `spotify` | Playback lag semantics and search quirks. |
| `notion` | Block-based editing semantics; handling of Return inside different block types. |

App-specific hints (`server/app-hints/*.md`) are different from
skills: they get auto-injected as `<app_hints>` inside every
`get_app_state` response for the matching app, so the model sees them
even if the containing skill isn't active. Skills are the
recommended entry point; app-hints are the safety net for app-
specific quirks.
