"""ClaudeBrain — runs the user's own Claude, wired to Otto's voice/screen.

This is deliberately thin. It does NOT define a persona, subagents, or MCP servers of its
own. Instead it loads the user's Claude configuration (settings, MCP servers, subagents,
skills, CLAUDE.md, permissions) via ``setting_sources`` and uses the ``claude_code`` system
prompt preset — so Otto behaves exactly like the user's Claude, just spoken. The only thing
Otto injects is a short voice-manners note and its voice/screen tools.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from ..config import Settings
from .tools import AssistantContext, build_server

# Appended to Claude's own system prompt — voice manners only, no persona override.
VOICE_WRAPPER = """You are the user's own Claude Code, with a voice and a screen added. The \
user TALKS to you instead of typing, and HEARS a short spoken reply; the dashboard shows this \
session's real chat — your tool calls, output, and code — exactly like a normal Claude Code \
window. Voice is just a new way to give you input; it does NOT change how you work.

YOU MANAGE THE WHOLE MACHINE, NOT ONE PROJECT. Think of yourself as the user's system-level \
assistant for their entire Mac — all of their projects AND the laptop itself. By DEFAULT answer \
at that system level: when the user asks something general ("what's going on", "what should we do", \
"brief me", "any suggestions"), speak about the WHOLE picture across projects and the machine, not \
just the current directory. A cross-project overview is injected when relevant — use it. Only dive \
into a single project when the user clearly points you at one ("look into <project>", "in <project>, \
do X"); then work in that project until they move on. Never open a general reply by fixating on one \
project as if it's the only thing that exists.

DO YOUR NORMAL WORK, SHOWN IN THE CHAT. For coding, commands, files, git, debugging, or any \
real task: just do it the way you normally would in Claude Code. Your tool calls and their \
results are visible to the user in the chat window — that IS the display. Do NOT re-narrate, \
re-summarize, or re-render your work into a fancy card. Don't read commands, file contents, \
diffs, logs, or long lists out loud.

SPEAK LIKE A SHARP, WARM COLLEAGUE — LEAD WITH THE ANSWER. Your spoken reply (your final \
assistant text) is read aloud by text-to-speech, so keep it to ONE or TWO short, natural \
sentences and put the outcome or the key number FIRST. Talk like a person: use contractions, \
be direct and decisive, no hedging ("I think maybe…"), no robotic preambles ("As an AI…", \
"Sure, I can help with that"). No markdown, lists, code, or file paths in what you say.

MATCH THE USER'S LANGUAGE. Reply in whatever language and register the user speaks to you in. If \
they use Hindi or Hinglish (Hindi–English code-switching, "accha theek hai, ye file kholo"), reply \
naturally in the same Hinglish — do NOT force it back to formal English or pure Hindi; mirror their \
mix. Indian English stays Indian English. When they switch, you switch. Keep it the same short, \
warm, spoken style in any language. IMPORTANT for voice: write Hindi/Hinglish in ROMANIZED Latin \
script ("theek hai, kaam ho gaya"), NOT Devanagari (देवनागरी) — your reply is read aloud by a \
neural voice that speaks Latin text naturally but can't pronounce Devanagari, so Latin keeps you \
sounding natural.

ALWAYS GIVE CLOSURE — DON'T END WITH "IT'S ON SCREEN". Every spoken reply must stand on its own \
as a complete thought: state the OUTCOME and the takeaway in words, then a next step if there is \
one. "It's on screen" is NOT an ending — it leaves the user hanging. You may add a brief pointer \
to the screen for LONG detail you're deliberately not reading (a big diff, a long list), but only \
AFTER you've said the actual conclusion, and never as the whole reply. Bad: "Done — the rest is on \
screen." Good: "Fixed the login bug — it was a stale token; the diff's on screen if you want it." \
Don't read long content aloud, but always say what it MEANS.

YOUR MANNER — a calm, capable flight-deck co-pilot. Warm and familiar, lightly confident, a \
touch of dry wit; never sycophantic, never bubbly. Address the user directly and naturally. \
When a task is done, END by handing them the next move when there's an obvious one ("Want me to \
open the diff?", "Shall I push it?") — a single offer, not a menu. Name specifics like \
call-signs: the concrete file, PR number, test count, or branch, not vague summaries.

REACT LIKE A PERSON — READ THE SITUATION, DON'T ANSWER FLAT. Before you respond, gauge what KIND \
of thing this is and let it shape your tone, urgency, and what you do — the way a sharp colleague \
would:
• Effort / scope: if a request needs real digging (a large project, "read all the docs and \
  recommend next steps"), FIRST size it up out loud and offer the choice — "That one's big, \
  ~40 docs; want a quick take now, or a few minutes for a proper read?" — then, if they want \
  thorough, go do it (in the background if it's long) and deliver when done. Don't silently grind \
  for minutes.
• Time-sensitivity: if something's time-critical or overdue (a deadline, a failing prod check, \
  "before the demo"), convey urgency and lead with it — "That's time-sensitive, let's do it now."
• Priority / importance: weight things — lead with what matters most, and say plainly when \
  something is minor or can wait, rather than treating everything as equal.
• Buy-in from others: if an action affects other people or needs someone's sign-off (a shared \
  resource, a team decision, anything public or hard to walk back), flag it — "This touches the \
  shared branch; worth a heads-up to the team first?" — instead of just doing it.
• Excitement / wins: when something genuinely lands well (a feature shipped, all tests green, a \
  milestone), show real (measured) enthusiasm — "Nice — that's the whole pipeline green."
• Risk / concern: when something looks risky or off, say so calmly and early, don't bury it.
• Empathy: if the user sounds rushed, stuck, or frustrated, acknowledge it briefly before \
  diving into the fix.
Keep all of this natural and SHORT (it's spoken) — a reaction is a phrase, not a paragraph.

WHEN ASKED FOR A STATUS OR BRIEF ("what's up", "brief me", "where are we") — give a crisp \
flight-deck rundown in one or two sentences: lead with the overall state (all green / one thing \
needs you), then the single most important item with its number (the open PR, the failing test, \
the branch just merged), and offer the obvious next step. Fast facts you can read quickly — git \
status, test results, changed files — are worth checking before you answer; don't guess.

BE PROACTIVE AND DECISIVE — PROCEED ON SENSIBLE DEFAULTS. Don't ask permission for ordinary, \
reversible steps; just do them and report the result. When something's ambiguous, make the \
reasonable assumption and say what you assumed, rather than stalling with a question. Ask (with \
the ask_user tool, then wait) only for ONE genuinely consequential or irreversible decision — \
and make it a single focused question, not a checklist. Skip filler; don't announce "one sec" \
or "let me check" before every tool. For a task that takes several steps (browsing, multi-file \
work), say a quick one-line intent, then keep working through the steps and give the result at \
the end — don't narrate each click.

DESKTOP CONTROL — YOU CAN DRIVE THE WHOLE MAC. You have desktop tools (take a screenshot, click, \
type text, press keys, scroll, drag, activate/open an app, run AppleScript). Use whichever your \
tool list actually exposes to operate ANY app the user asks about — Finder, VS Code, IntelliJ, \
Calculator, Chrome, Slack, whatever — not just the terminal. Typical loop: screenshot to see the \
screen, then click/type to act; bring the app to the front first. For web tasks needing the user's \
own accounts or sessions ("use my browser", posting to a logged-in site), DRIVE THEIR REAL CHROME \
(bring Chrome to the front → act) — it already has their logins. Do NOT use Playwright for that: it \
launches a separate, isolated Chromium with none of their sessions and hits login walls; \
Playwright/headless is only for throwaway automation that needs no login. If an action fails on a \
permission (Accessibility / Screen Recording), report it and tell the user exactly what to enable \
— don't silently give up or fall back to an isolated browser.

CAPABILITIES ARE COMPOSED FROM YOUR GENERAL TOOLS — DON'T SAY "I HAVE NO TOOL FOR THAT." You don't \
need a purpose-named tool to do something; you have run_command (any CLI), run_applescript (any \
scriptable app), open/activate_app, screenshot, click, type_text and key_press — compose them. \
Before answering "I can't", ask what shell command or app would do it, then do THAT. Examples: \
CAMERA / WEBCAM — you CAN take a photo or record video even with no "camera" tool: open Photo Booth \
(`open -a "Photo Booth"`) and click its shutter, OR script QuickTime via run_applescript (`tell \
application "QuickTime Player" to start (new movie recording)` … later `stop`), OR run_command with \
ffmpeg (`ffmpeg -f avfoundation -framerate 30 -i "0" -t <secs> <file>` for video, add an image2 \
output for a still). AUDIO recording, PDFs, screenshots-to-file, resizing images, converting media, \
reading a webpage's text, controlling Music/Spotify/Notes/Calendar — all reachable via \
run_command/run_applescript. The camera and Automation permissions prompt on first use; if a real \
permission or a missing binary blocks you, say exactly which — but never decline something you could \
do just because there's no tool named for it.

REUSE THE CURRENT BROWSER TAB — DON'T PILE UP TABS. When you search or play media (a song, a video, \
a YouTube result) and again on every follow-up ("play the next one", "try another"), work in the \
tab that's ALREADY open — do NOT open a new tab each time. The plain `open <url>` / open-app path \
spawns a fresh tab every call, which is exactly the "it keeps opening new tabs" problem; avoid it \
for this. Instead reuse the active tab: navigate it with AppleScript — e.g. `tell application \
"Google Chrome" to set URL of active tab of front window to "<url>"`.

PLAY MEDIA BY URL YOU KNOW, NOT BY CLICKING. To actually PLAY a song/video, navigate the ACTIVE \
tab straight to a watch/track URL — never screenshot-and-click. Screenshot-and-click is unreliable \
and OUTRIGHT FAILS when the user has an external monitor: the screenshot tool captures only the \
MAIN display, so a target on the second screen is invisible ("visibility wall"). The reliable, \
display-independent path that needs NO Chrome setting: use YOUR OWN knowledge of the song to build \
a direct YouTube watch URL (e.g. Hey Jude → https://www.youtube.com/watch?v=A_MjCqQoLLA) and set \
it as the active tab's URL — `tell application "Google Chrome" to set URL of active tab of front \
window to "<watch url>"` — a watch URL autoplays. Setting the tab URL via AppleScript ALWAYS works \
(unlike executing JavaScript in Chrome, which is off by default). Only if you genuinely don't know \
a watch URL: navigate to the search results URL and either tell the user to pick one, or — if they \
turn on Chrome's "View › Developer › Allow JavaScript from Apple Events" — read the first result's \
href with `execute active tab … javascript` and navigate there. One tab, reused; playback by a URL \
you know, not by pixel-clicks.

ANSWER NOW — NEVER PROMISE TO FOLLOW UP LATER. You run only during this turn; you CANNOT send a \
message later, watch something in the background, or notify the user when something finishes. So \
never say "I'll let you know", "I'll get back to you", "I'll flag you when it lands", or similar \
— that reply never comes and looks like you froze. Instead, finish the check within this turn and \
report what you found; if something is genuinely still running or idle, say its CURRENT state in \
one sentence and tell the user to just ask again when they want a fresh check.

render_canvas is OPTIONAL — a bonus screen, not a requirement. Use it ONLY when a visual is \
clearly better than the chat: a chart or table of data, a status dashboard, a diagram of how \
something works, a countdown/clock/weather scene, or a single big highlighted answer to a \
simple factual question. For ordinary coding and command work, do NOT call render_canvas — let \
the chat show the work. When you do render, make it self-contained HTML on a DARK theme \
(dark/transparent background, near-white text, cyan #22d3ee + purple #a855f7 accents, big \
legible type, rounded cards — never a plain white page).

Everything else about how you work — your tools, subagents, skills, project instructions — is \
unchanged."""


class ClaudeBrain:
    def __init__(self, ctx: AssistantContext, settings: Settings, *,
                 resume: str | None = None, new_chat: bool = False,
                 cwd: str | None = None, read_only: bool = False) -> None:
        self.ctx = ctx
        self.settings = settings
        self.client: ClaudeSDKClient | None = None
        # Read-only mode (idle background research): hard-block edits/writes regardless of the
        # user's permission setting — see connect() and _permission.
        self._read_only = read_only
        # Per-workspace working directory (overrides the global settings.project_path so
        # several workspaces can run in different project dirs concurrently).
        self._cwd = cwd
        # Which chat (Claude session) this brain is bound to:
        #   resume=<id> -> attach to that existing chat; new_chat=True -> a fresh chat;
        #   neither -> honor the global continue_conversation setting (today's behavior).
        self._resume = resume
        self._new_chat = new_chat
        # The live chat id — pre-seeded for a resume, else captured from the first turn.
        self.session_id: str | None = resume

    def _mcp_servers(self) -> dict[str, Any]:
        """The MCP servers Orrin provides to the brain directly (always loaded, no Claude Code
        approval gating): its own voice/screen tools, and — when enabled — the mac-use-mcp
        'computer-use' desktop-automation server so the brain can click/type/focus/open ANY app."""
        servers: dict[str, Any] = {"voice": build_server(self.ctx)}
        b = self.settings.brain
        # Unique name "desktop" (not "computer-use") so it can't collide with — or inherit the
        # approval gating of — the user's own ~/.claude "computer-use" entry.
        if getattr(b, "computer_use", False) and b.computer_use_command:
            self._ensure_desktop_helper_executable(b.computer_use_args or [])
            servers["desktop"] = {
                "type": "stdio",
                "command": b.computer_use_command,
                "args": list(b.computer_use_args or []),
            }
        return servers

    @staticmethod
    def _ensure_desktop_helper_executable(args: list[str]) -> None:
        """mac-use-mcp ships its Swift `input-helper` binary without the exec bit surviving an
        npm install, so every screen-read/click tool fails its X_OK pre-check with a misleading
        'helper binary not found'. Self-heal: mark it executable before we spawn the server.
        Derives the module dir from the dist/index.js path we invoke it with."""
        import os
        import stat

        for a in args:
            if a.endswith("index.js") and os.path.sep in a:
                helper = os.path.join(os.path.dirname(os.path.dirname(a)), "bin", "input-helper")
                try:
                    if os.path.isfile(helper) and not os.access(helper, os.X_OK):
                        st = os.stat(helper).st_mode
                        os.chmod(helper, st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                except OSError:
                    pass  # best-effort; the server itself will surface a real error if it matters
                break

    async def connect(self) -> None:
        b = self.settings.brain
        # Session binding is mutually exclusive: a specific chat (resume) OR a brand-new chat
        # OR continue-latest. Building it as a dict-spread keeps the SDK from ever getting a
        # conflicting pair (resume + continue_conversation).
        if self._new_chat:
            session_opts: dict[str, Any] = {}
        elif self._resume:
            # fork_session=False → keep writing to the SAME session id instead of forking a
            # new one on resume. Otherwise Otto's turns land in a fresh session file and never
            # show up in the chat the user reopens in Claude Code for that project.
            session_opts = {"resume": self._resume, "fork_session": False}
        else:
            session_opts = {"continue_conversation": b.continue_conversation}
        # Let Claude read this project's memory (saved docs, project.md, summary) in full when
        # it wants more than the injected preamble excerpt. Only if it exists (nothing to read
        # otherwise), and behind the config flag.
        extra_dirs = list(self.settings.add_dirs)
        if self.settings.memory.enabled and self.settings.memory.expose_to_claude:
            try:
                from ..config import OTTO_HOME
                from ..memory import project_slug
                cwd = self._cwd or str(self.settings.project_path)
                memdir = OTTO_HOME / "memory" / "projects" / project_slug(cwd)
                if memdir.exists():
                    extra_dirs.append(str(memdir))
                # Cross-project: optionally let the brain read every project's memory notes.
                if self.settings.memory.cross_project and self.settings.memory.expose_overview_dirs:
                    projroot = OTTO_HOME / "memory" / "projects"
                    if projroot.exists():
                        extra_dirs.append(str(projroot))
            except Exception:
                pass
        # Read-only research: force the SDK's own gating ON (so _permission runs) and hard-block
        # the built-in editors at the SDK level — belt and suspenders, since the user's normal
        # setting is bypass (which would otherwise auto-approve everything).
        if self._read_only:
            perm_mode = "default"
            disallowed = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
        else:
            perm_mode = ("bypassPermissions"
                         if b.permission in ("bypass", "auto") else "default")
            disallowed = []
        options = ClaudeAgentOptions(
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": VOICE_WRAPPER,
            },
            # Load the user's own Claude setup: MCP servers, subagents, skills, CLAUDE.md.
            setting_sources=(["user", "project", "local"] if b.use_claude_config else []),
            # Otto's voice/screen tools ("voice"), plus — so the brain can actually DRIVE the
            # whole desktop (click/type/focus/open any app, use the user's real logged-in Chrome)
            # — the computer-use automation MCP loaded DIRECTLY here. Loading it via the SDK
            # (rather than relying on the user's ~/.claude mcpServers) guarantees it's present and
            # not gated by Claude Code's per-server approval, which was leaving the headless brain
            # with only the read-only macOS tools.
            mcp_servers=self._mcp_servers(),
            can_use_tool=self._permission,
            cwd=(self._cwd or str(self.settings.project_path)),
            add_dirs=extra_dirs,
            include_partial_messages=True,
            # Full-access sessions skip the SDK's own gating too; other levels go through the
            # default flow where can_use_tool (_permission) asks/allows/denies per the setting.
            permission_mode=perm_mode,
            disallowed_tools=disallowed,
            # The SDK reads the CLI's stream-json output line by line with a default 1 MB cap
            # per message. A single big tool result (a browser screenshot/DOM, a large file
            # read) can exceed that, raising CLIJSONDecodeError and corrupting the stream — the
            # brain then returns empty for every later turn. Give it generous headroom.
            max_buffer_size=64 * 1024 * 1024,
            **session_opts,
            **({"model": b.model} if b.model else {}),
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()

    async def disconnect(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def reconnect(self) -> None:
        """Rebuild the SDK client after a fatal stream error (e.g. a tool result that overflowed
        the JSON buffer corrupts the stream, so every later turn returns empty). Resume the same
        chat where possible so the conversation continues instead of starting over."""
        if self.session_id:
            self._resume = self.session_id
            self._new_chat = False
        await self.disconnect()
        await self.connect()

    async def interrupt(self) -> None:
        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def _permission(self, tool_name: str, input_data: dict[str, Any], context: Any):
        """Route Claude's permission prompts to voice/UI per the wiring setting.

        Only invoked for tools the user's own Claude rules didn't already auto-approve.
        Otto's own voice/screen tools are always allowed.
        """
        if tool_name.startswith("mcp__voice__"):
            return PermissionResultAllow()          # Orrin's own tools (remember/say/notify)
        # Read-only research mode: allow only reads + Orrin's tools; deny every write/action
        # (editors are also SDK-disallowed, so this covers Bash-writes, sends, browser/computer
        # loops, and anything unknown). This runs unattended, so it defaults to DENY.
        if self._read_only:
            if _is_read_only(tool_name):
                return PermissionResultAllow()
            if tool_name == "Bash" and _bash_is_read_only(input_data.get("command", "")):
                return PermissionResultAllow()
            return PermissionResultDeny(message="Read-only research mode: writes are blocked.")
        mode = self.settings.brain.permission
        # Deny mode denies everything (except Orrin's own voice tools, handled above) — defence
        # in depth: it must NOT depend on the read-only heuristic classifying correctly, or a
        # misclassified write (#48) would slip through. Checked before any auto-allow.
        if mode == "deny":
            return PermissionResultDeny(message="Denied by Otto's permission setting.")
        # Agentic action tools (browser / computer-use) run through their loop without a
        # per-step confirmation — otherwise every click would need a spoken yes, which makes
        # computer use unusable. Configurable via brain.auto_allow_tools.
        if any(tool_name.startswith(p) for p in (self.settings.brain.auto_allow_tools or ())):
            return PermissionResultAllow()
        # Read-only tools (searching Gmail, listing files, web search…) never need a
        # confirmation — only consequential actions do.
        if _is_read_only(tool_name):
            return PermissionResultAllow()
        if tool_name == "Bash" and _bash_is_read_only(input_data.get("command", "")):
            return PermissionResultAllow()
        if mode in ("bypass", "auto"):
            return PermissionResultAllow()
        if mode == "guarded":
            # Do everything asked; only stop for genuinely DANGEROUS actions (destructive
            # shell, delete/erase, outbound send, payments) and confirm those by voice.
            if not _is_dangerous(tool_name, input_data):
                return PermissionResultAllow()
        ok = await self.ctx.confirm(_describe(tool_name, input_data))
        return PermissionResultAllow() if ok else PermissionResultDeny(
            message="The user declined this action."
        )

    async def send(self, text: str) -> AsyncIterator[dict[str, Any]]:
        assert self.client is not None, "Brain not connected"
        await self.client.query(text)
        delta_emitted = False
        text_blocks: list[str] = []
        async for message in self.client.receive_response():
            if isinstance(message, StreamEvent):
                delta = _extract_text_delta(message.event)
                if delta:
                    delta_emitted = True
                    yield {"type": "delta", "text": delta}
            elif isinstance(message, AssistantMessage):
                # Capture the chat id early — this is how a brand-new chat learns its id.
                if getattr(message, "session_id", None):
                    self.session_id = message.session_id
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_blocks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "detail": _tool_detail(block.name, block.input),
                        }
            elif isinstance(message, ResultMessage):
                if getattr(message, "session_id", None):
                    self.session_id = message.session_id
                full = "".join(text_blocks).strip()
                if not delta_emitted and full:
                    yield {"type": "delta", "text": full}
                yield {
                    "type": "result",
                    "is_error": message.is_error,
                    "cost": message.total_cost_usd,
                    "error_text": full if message.is_error else "",
                    "session_id": self.session_id,
                }


# Built-in tools that only read; and verb patterns for classifying MCP tools.
# Task (subagent spawn) is deliberately NOT here: a subagent can perform consequential tool
# calls that may not re-enter this confirmation, so spawning one is itself consequential.
_READ_BUILTINS = {"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "TodoWrite",
                  "NotebookRead"}
# Write verbs, matched as a whole leading leaf token in _is_read_only (e.g. send_message,
# remove_label). A write head wins over any read classification.
_WRITE_VERBS = re.compile(
    r"(send|create|delete|remove|update|write|edit|trash|move|archive|reply|forward|"
    r"draft|post|add|set|modify|label|insert|upload|share|rename|revoke|cancel|pay|buy)",
    re.I)


# Shell commands that only read state — safe to run without a spoken confirmation.
# NOT read-only, so deliberately excluded: `find` (can `-delete`/`-exec` arbitrary commands)
# and `open` (launches apps/URLs — a side-effecting exec, not a read).
# Includes common macOS status readers (pmset/sw_vers/sysctl/vm_stat/…) and the text filters
# people pipe status through (awk/sed/cut/sort/…) so a spoken "how's my battery and disk?"
# — which becomes a `pmset -g batt | grep … ; df -H / | awk …` pipeline — runs without a
# confirmation. awk/sed can still WRITE, but only via redirection or -i, both blocked below.
# Deliberately EXCLUDED (they read-classify as True too easily but can execute or write, #48):
#   env / printenv — `env python3 -c …` runs any program; both print API keys into the transcript
#   awk / sed      — Turing-complete: awk `system(...)` / pipe-to-shell, sed `w`/`e` commands,
#                    both hard to vet with a regex, so they always ask (or run under bypass)
#   rg             — `rg --pre <prog>` executes an arbitrary helper per file
# Pure text filters below can't exec; their only write vector (`-o` / `>`) is vetoed in _BASH_DANGER.
_BASH_READ_CMDS = {"date", "ls", "pwd", "cat", "head", "tail", "echo", "printf", "whoami",
                   "df", "du", "uptime", "cal", "which", "type",
                   "hostname", "uname", "arch", "sw_vers", "vm_stat", "sysctl", "ps",
                   "pmset",  # guarded to `-g` (get) by _BASH_DANGER; other forms mutate power
                   "id", "groups", "basename", "dirname", "realpath", "seq",
                   "wc", "grep", "egrep", "fgrep", "cut", "tr",
                   "sort", "uniq", "column", "rev", "stat", "file"}
# Anything that mutates state, escalates, reaches the network, sleeps/powers the machine, or
# runs an arbitrary command — checked across the WHOLE command string so it can't hide in a
# later pipe segment. Shell operators (| ; & && ||) are NOT here: they're split out and each
# segment is validated on its own, which is stricter than a blanket veto.
_BASH_DANGER = re.compile(
    r"(`|\$\(|[<>]|\brm\b|\bsudo\b|\bmv\b|\bcp\b|\bkill\b|\bpkill\b|\bdd\b|\btee\b|\bln\b|"
    r"\bchmod\b|\bchown\b|\bcurl\b|\bwget\b|\bnc\b|\bssh\b|\bscp\b|\bxargs\b|\beval\b|"
    r"\bosascript\b|\blaunchctl\b|\bdiskutil\b|\btmutil\b|\bnvram\b|\bshutdown\b|\breboot\b|"
    r"\b\w+\s+-i\b|"                                  # in-place edit, e.g. sed -i / perl -i
    r"\bpmset\b(?!\s+-g)|\bsysctl\b(?=\s+-w)|"        # pmset only for -g (get); no sysctl -w
    r"\bsystem\s*\(|\bENVIRON\b|"                     # awk/perl inline exec + env-secret access
    r"\bsort\b[^|&;]*\s-o\b|--output(=|\s|\b)|"       # write-to-file flags (sort -o, --output)
    r"\|\s*[\"']?\s*(sh|bash|zsh|/bin/|python|perl|ruby|node)\b|"   # pipe to a shell/interpreter
    r"\bgit\s+(push|commit|reset|clean|rebase|checkout|restore)|"
    r"\bgh\s+\w+\s+(create|delete|edit|merge|close))")
# Split on shell operators AND newlines/carriage-returns: `ls\npython3 -c '…'` must be checked as
# two segments, not slip through on the leading `ls` (#48).
_BASH_SPLIT = re.compile(r"\|\||&&|[|;&\n\r]")


def _bash_is_read_only(command: str) -> bool:
    cmd = command.strip()
    if not cmd or _BASH_DANGER.search(cmd):
        return False
    # Reject anything with control characters we don't handle (NUL, vertical tab, form-feed, …);
    # they can hide a second command from a naive parser. \n and \r are split on below.
    if any(ord(c) < 32 and c not in "\t\n\r" for c in cmd):
        return False
    # A compound command is read-only only if EVERY segment is. Splitting and checking each
    # segment's leading word is stricter than the old single-word check: `df | xargs rm` now
    # fails on the `xargs`/`rm` segment instead of passing on `df`.
    for seg in _BASH_SPLIT.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        first = seg.split()[0]
        if first in _BASH_READ_CMDS:
            continue
        # read-only subcommands of common tools
        if re.match(r"^(git\s+(status|log|diff|show|branch)|gh\s+\w+\s+(list|view|status)|"
                    r"defaults\s+read|scutil\s+--get)\b", seg):
            continue
        return False
    return True


# Leading-token verbs for MCP leaf names. Matched as WHOLE tokens at the START of the name only,
# never as substrings — `execute_query` must not read-classify because it contains "query", and
# `approve_details` must not because it contains "details" (#48). query/execute/run/exec/call are
# actions, not reads, even though a server might name a read tool `*_query`; ask on those.
_READ_HEADS = {"list", "get", "read", "fetch", "view", "find", "search", "show", "describe",
               "lookup", "count", "status", "summary", "summarize", "recent", "history",
               "browse", "inspect", "check", "peek", "preview"}
_NONREAD_HEADS = {"query", "execute", "exec", "run", "call", "invoke", "eval", "approve",
                  "confirm", "send", "create", "delete", "update", "write", "set"}


def _leaf_tokens(leaf: str) -> list[str]:
    """Split an MCP leaf name into lowercase word tokens across snake_case AND camelCase, so
    `getThread` and `get_thread` both tokenize to ['get', 'thread']."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", leaf).replace("-", "_")
    return [p.lower() for p in spaced.split("_") if p]


def _is_read_only(tool_name: str) -> bool:
    """Best-effort: does this tool only read? Reads skip the confirmation prompt. Conservative —
    anything unrecognised is treated as NOT read-only (ask), and this is a convenience heuristic,
    not a security boundary (deny/read-only modes still deny unknowns)."""
    if tool_name in _READ_BUILTINS:
        return True
    if tool_name.startswith("mcp__"):
        tokens = _leaf_tokens(tool_name.split("__")[-1])
        if not tokens:
            return False
        head = tokens[0]
        if head in _NONREAD_HEADS or _WRITE_VERBS.fullmatch(head):
            return False
        return head in _READ_HEADS
    return False


# Genuinely dangerous / irreversible / outbound actions — confirmed even in "guarded" mode
# (which otherwise runs everything asked without asking). Normal work (editing files, opening
# apps, clicking, searching, reading) is NOT here and runs freely.
_DANGEROUS_BASH = re.compile(
    r"\brm\s+-\w*[rf]|\brmdir\b|\bdd\b|\bmkfs\w*|\bdiskutil\s+(erase|reformat|partition)|"
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bkillall\b|\bpkill\b|\bkill\s+-9\b|\bsudo\b|"
    r"\bchmod\s+-R\b|\bchown\s+-R\b|>\s*/dev/|\bnvram\b|\btmutil\s+delete\b|"
    r"\bdefaults\s+delete\b|\blaunchctl\s+(unload|remove|bootout)\b|"
    r"\bgit\s+push\b.*(--force|-f)\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-\w*f|"
    r"\bnpm\s+publish\b|\bbrew\s+uninstall\b", re.I)
_DANGEROUS_VERBS = re.compile(
    r"(delete|destroy|erase|purge|\bdrop\b|trash|wipe|"             # destructive
    r"send|publish|\bpost\b|forward|reply|tweet|broadcast|"         # outbound comms
    r"\bpay\b|\bbuy\b|purchase|checkout|transfer|refund|charge|"    # money
    r"revoke|uninstall)", re.I)


def _is_dangerous(tool_name: str, args: dict[str, Any]) -> bool:
    """True only for genuinely destructive/irreversible/outbound actions that warrant a spoken
    confirmation even when Otto is set to do everything else without asking (guarded mode)."""
    if tool_name == "Bash":
        return bool(_DANGEROUS_BASH.search(str(args.get("command", ""))))
    # Built-in file editors (Write/Edit/etc.) are normal asked work — not gated here.
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return False
    leaf = tool_name.split("__")[-1].lower()
    return bool(_DANGEROUS_VERBS.search(leaf))


def _describe(tool_name: str, args: dict[str, Any]) -> str:
    """A natural, spoken-friendly description of what a tool will DO and its impact — NOT the
    raw command (that's already shown on screen in the chat). Used for the voice confirmation."""
    import os as _os

    if tool_name == "Bash":
        return _describe_bash(str(args.get("command", "")))
    if tool_name == "Write":
        f = _os.path.basename(args.get("file_path", args.get("path", "")) or "a file")
        return f"create or overwrite {f} — this writes a file to disk"
    if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        f = _os.path.basename(args.get("file_path", args.get("path", "")) or "a file")
        return f"edit {f} — this changes a file on disk"
    if tool_name.startswith("mcp__"):
        leaf = tool_name.split("__")[-1].replace("_", " ").strip()
        return leaf or "use a connected tool"
    return f"use the {tool_name} tool"


def _describe_bash(command: str) -> str:
    """Summarize a shell command as intent + impact, in one short spoken sentence."""
    c = " ".join(command.split())
    low = c.lower()
    # Destructive / irreversible — call it out clearly.
    if re.search(r"\brm\s+-?[rf]|\bgit\s+reset\s+--hard|\bgit\s+clean\b|\bdd\b|\bmkfs|"
                 r"\bdrop\s+(table|database)\b|\btruncate\b|>\s*/dev/|\bshred\b", low):
        return "run a command that deletes or overwrites data — this can't easily be undone"
    if re.search(r"\bgit\s+push\b", low):
        force = "force-" if re.search(r"--force|-f\b|force-with-lease", low) else ""
        return f"{force}push your commits to the remote — this publishes them"
    if re.search(r"\bgh\s+(pr|release|repo|issue)\s+\w*\s*(create|merge|close|delete|edit)", low):
        return "run a GitHub command that changes a pull request, release, or the remote"
    if re.search(r"\bgit\s+commit\b", low):
        return "commit the current changes to git"
    if re.search(r"\b(npm|pnpm|yarn|pip|pip3|brew|uv|cargo|gem|apt|go)\s+(install|add|i|get)\b", low):
        return "install software or dependencies onto your machine"
    if re.search(r"\bkill|pkill|killall\b", low):
        return "stop a running process"
    if re.search(r"\b(curl|wget)\b", low):
        return "make a network request"
    if re.search(r"\b(mv|cp|mkdir|touch|chmod|chown|ln)\b|>>?\s*\S", low):
        return "create or change files on disk"
    # Read-only-ish or unknown: name the tool without reading the whole command.
    parts = [p for p in re.split(r"\s*(?:&&|\|\||;|\|)\s*", c) if p.strip()]
    first = parts[0].split()[0] if parts and parts[0].split() else "a shell"
    if len(parts) > 1:
        return f"run a few shell commands (starting with {first})"
    return f"run the {first} command"


def _extract_text_delta(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""


def _tool_detail(name: str, args: dict[str, Any]) -> str:
    if name == "Task":
        return f"{args.get('subagent_type', 'subagent')}: {args.get('description', '')}"
    if name == "Bash":
        return str(args.get("command", ""))[:120]
    if name.startswith("mcp__voice__"):
        return ""
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2])
