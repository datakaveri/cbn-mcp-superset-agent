"""
Terminal User Interface for the Superset MCP Agentic Pipeline.
Built with the blessed library — a gorgeous, color-coded dashboard.
"""

import sys
import threading
import time
from collections import deque
from datetime import datetime

import blessed

from models import Phase, PipelineReport
from pipeline import Pipeline

# ── Color Palette (matching the architecture diagram) ──────────────
# Purple  = orchestrator/LLM
# Green   = agents/success
# Blue    = MCP tools
# Orange  = auth/warnings
# Red     = errors
# Gray    = neutral


class TUI:
    """
    Full-screen terminal UI with panels for input, pipeline progress,
    live logs, and results.
    """

    def __init__(self):
        self.term = blessed.Terminal()
        self.pipeline: Pipeline | None = None

        # State
        self.input_buffer = ""
        self.cursor_pos = 0
        self.logs: deque[dict] = deque(maxlen=500)
        self.phase_status: dict[Phase, str] = {}  # phase → "pending"|"running"|"done"|"error"
        self.report: PipelineReport | None = None
        self.is_running = False
        self.health_ok = {"mcp": False, "superset": False, "llm": False}
        self.scroll_offset = 0
        self.show_help = False
        self.status_message = "Ready — type a query and press Enter"
        self.start_time: float | None = None

        # Panel dimensions (computed on render)
        self.w = 0
        self.h = 0

    # ── Entry Point ──────────────────────────────────────────────────

    def run(self):
        """Launch the TUI — blocks until quit."""
        with self.term.fullscreen(), self.term.cbreak(), self.term.hidden_cursor():
            self._draw_full()
            self._input_loop()

    # ── Input Loop ───────────────────────────────────────────────────

    def _input_loop(self):
        """Main event loop — handles keyboard input."""
        while True:
            key = self.term.inkey(timeout=0.1)

            if key == "":
                # Timeout — refresh display if pipeline is running
                if self.is_running:
                    self._draw_full()
                continue

            if key.name == "KEY_ESCAPE" or (key == "q" and not self.is_running):
                break

            if self.show_help:
                self.show_help = False
                self._draw_full()
                continue

            if key == "?" and not self.is_running:
                self.show_help = True
                self._draw_full()
                continue

            if key.name == "KEY_ENTER":
                if self.input_buffer.strip() and not self.is_running:
                    self._execute_query(self.input_buffer.strip())
                    self.input_buffer = ""
                    self.cursor_pos = 0
                continue

            if key.name == "KEY_BACKSPACE" or key.name == "KEY_DELETE":
                if self.cursor_pos > 0:
                    self.input_buffer = (
                        self.input_buffer[:self.cursor_pos - 1]
                        + self.input_buffer[self.cursor_pos:]
                    )
                    self.cursor_pos -= 1
                    self._draw_input_panel()
                continue

            if key.name == "KEY_LEFT":
                self.cursor_pos = max(0, self.cursor_pos - 1)
                self._draw_input_panel()
                continue

            if key.name == "KEY_RIGHT":
                self.cursor_pos = min(len(self.input_buffer), self.cursor_pos + 1)
                self._draw_input_panel()
                continue

            if key.name == "KEY_UP":
                self.scroll_offset = min(self.scroll_offset + 1, max(0, len(self.logs) - 5))
                self._draw_log_panel()
                continue

            if key.name == "KEY_DOWN":
                self.scroll_offset = max(0, self.scroll_offset - 1)
                self._draw_log_panel()
                continue

            # Ctrl+C
            if key == chr(3):
                break

            # Ctrl+L — clear
            if key == chr(12):
                self.logs.clear()
                self.report = None
                self.phase_status.clear()
                self.scroll_offset = 0
                self.status_message = "Cleared — ready for new query"
                self._draw_full()
                continue

            # Regular character input
            if key.is_sequence is False and len(key) == 1 and not self.is_running:
                self.input_buffer = (
                    self.input_buffer[:self.cursor_pos]
                    + key
                    + self.input_buffer[self.cursor_pos:]
                )
                self.cursor_pos += 1
                self._draw_input_panel()

    # ── Pipeline Execution ───────────────────────────────────────────

    def _execute_query(self, query: str):
        """Run the pipeline in a background thread."""
        self.is_running = True
        self.report = None
        self.logs.clear()
        self.phase_status.clear()
        self.scroll_offset = 0
        self.start_time = time.time()
        self.status_message = "Pipeline running..."
        self._draw_full()

        thread = threading.Thread(target=self._run_pipeline, args=(query,), daemon=True)
        thread.start()

    def _run_pipeline(self, query: str):
        """Background thread: runs the pipeline and updates state."""
        try:
            self.pipeline = Pipeline(on_progress=self._on_progress)
            report = self.pipeline.run(query)
            self.report = report
            if report.success:
                self.status_message = f"✅ Dashboard live → {report.dashboard_url}"
            else:
                self.status_message = f"⚠ Pipeline finished with {len(report.errors)} error(s)"
        except Exception as e:
            self._on_progress(Phase.HEALTH_CHECK, "error", f"Fatal: {e}")
            self.status_message = f"❌ Fatal error: {e}"
        finally:
            self.is_running = False
            if self.pipeline:
                self.pipeline.close()
            self._draw_full()

    def _on_progress(self, phase: Phase, level: str, message: str):
        """Callback from pipeline — thread-safe state update."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append({"ts": ts, "phase": phase, "level": level, "msg": message})

        # Update phase status
        if level == "error":
            self.phase_status[phase] = "error"
        elif level == "success" and phase not in self.phase_status:
            self.phase_status[phase] = "done"
        elif phase not in self.phase_status or self.phase_status[phase] not in ("done", "error"):
            self.phase_status[phase] = "running"

        # Mark done when a success comes after running
        if level == "success":
            self.phase_status[phase] = "done"

        self._draw_full()

    # ── Drawing ──────────────────────────────────────────────────────

    def _draw_full(self):
        """Redraw the entire screen."""
        self.w = self.term.width
        self.h = self.term.height

        if self.w < 40 or self.h < 15:
            print(self.term.home + self.term.clear + "Terminal too small. Resize to at least 40x15.")
            return

        output = []
        output.append(self.term.home + self.term.clear)

        # Header
        output.extend(self._render_header())
        # Input panel
        output.extend(self._render_input())
        # Pipeline progress (left) + Logs (right) — split view
        output.extend(self._render_body())
        # Result/status bar
        output.extend(self._render_footer())

        sys.stdout.write("".join(output))
        sys.stdout.flush()

    def _draw_input_panel(self):
        """Redraw just the input panel."""
        lines = self._render_input()
        sys.stdout.write("".join(lines))
        sys.stdout.flush()

    def _draw_log_panel(self):
        """Redraw the body area."""
        lines = self._render_body()
        sys.stdout.write("".join(lines))
        sys.stdout.flush()

    # ── Renderers ────────────────────────────────────────────────────

    def _render_header(self) -> list[str]:
        """Render the header bar with title and status indicators."""
        t = self.term
        parts = []

        # Line 1: Title bar
        title = " ◆ SUPERSET MCP AGENT PIPELINE "
        pad = self.w - len(title) - 30
        indicators = self._status_indicators()
        bar = f"{t.on_color_rgb(30, 25, 80)}{t.color_rgb(180, 170, 255)}{title}"
        bar += " " * max(0, pad)
        bar += indicators
        bar += " " * max(0, self.w - len(title) - max(0, pad) - len(self._strip_ansi(indicators)))
        bar += t.normal
        parts.append(t.move_xy(0, 0) + bar)

        # Line 2: Subtitle
        sub = f"  Natural Language → Superset Dashboards  │  gpt-20b @ 10.10.17.55  │  MCP @ 5008"
        parts.append(t.move_xy(0, 1) + t.color_rgb(120, 115, 160) + sub[:self.w] + t.normal)

        return parts

    def _status_indicators(self) -> str:
        """Render connection status dots."""
        t = self.term

        def dot(ok, label):
            color = t.color_rgb(80, 220, 150) if ok else t.color_rgb(100, 100, 90)
            return f" {color}●{t.normal} {t.color_rgb(160, 155, 180)}{label}{t.normal}"

        return (
            dot(self.health_ok.get("mcp"), "MCP")
            + dot(self.health_ok.get("superset"), "API")
            + dot(self.health_ok.get("llm"), "LLM")
        )

    def _render_input(self) -> list[str]:
        """Render the query input box."""
        t = self.term
        parts = []
        y = 3

        # Border top
        label = "─── QUERY "
        border_top = f"  ┌{label}{'─' * max(0, self.w - len(label) - 5)}┐"
        parts.append(t.move_xy(0, y) + t.color_rgb(100, 95, 180) + border_top + t.normal)

        # Input line
        prompt = "  │ ❯ "
        visible_width = self.w - len("  │ ❯ ") - 3
        display_text = self.input_buffer
        if len(display_text) > visible_width:
            display_text = display_text[-(visible_width):]
        pad = " " * max(0, visible_width - len(display_text))
        input_line = (
            f"{t.color_rgb(100, 95, 180)}{prompt}"
            f"{t.color_rgb(230, 225, 255)}{t.bold}{display_text}{t.normal}"
            f"{pad}"
            f"{t.color_rgb(100, 95, 180)} │{t.normal}"
        )
        parts.append(t.move_xy(0, y + 1) + input_line)

        # Border bottom
        hint = " Enter=run  ?=help  Ctrl+L=clear  q=quit "
        border_bot = f"  └{'─' * 3}{t.color_rgb(80, 75, 120)}{hint}{t.color_rgb(100, 95, 180)}{'─' * max(0, self.w - len(hint) - 8)}┘"
        parts.append(t.move_xy(0, y + 2) + t.color_rgb(100, 95, 180) + border_bot + t.normal)

        return parts

    def _render_body(self) -> list[str]:
        """Render the split body: pipeline phases (left) + logs (right)."""
        t = self.term
        parts = []
        y_start = 7
        body_height = self.h - y_start - 4  # Leave room for footer

        # Split: 35% phases, 65% logs
        phase_width = min(36, self.w // 3)
        log_width = self.w - phase_width - 4

        # ── Phase panel ──
        phase_label = "── PIPELINE ──"
        parts.append(
            t.move_xy(1, y_start)
            + t.color_rgb(80, 200, 150) + f"  {phase_label}" + t.normal
        )

        phases_ordered = [
            (Phase.HEALTH_CHECK, "Health Check"),
            (Phase.PLAN_GENERATION, "Plan Generation"),
            (Phase.DATASET_DISCOVERY, "Dataset Discovery"),
            (Phase.PLAN_REFINEMENT, "Plan Refinement"),
            (Phase.SQL_VALIDATION, "SQL Validation"),
            (Phase.CHART_CREATION, "Chart Creation"),
            (Phase.DASHBOARD_ASSEMBLY, "Dashboard Assembly"),
            (Phase.RESULT_REPORTING, "Result Reporting"),
        ]

        for i, (phase, label) in enumerate(phases_ordered):
            row_y = y_start + 1 + i
            if row_y >= y_start + body_height:
                break

            status = self.phase_status.get(phase, "pending")
            icon, color = {
                "pending":  ("○", t.color_rgb(80, 78, 70)),
                "running":  ("◉", t.color_rgb(255, 200, 60)),
                "done":     ("●", t.color_rgb(80, 220, 150)),
                "error":    ("✖", t.color_rgb(255, 90, 80)),
            }.get(status, ("○", t.color_rgb(80, 78, 70)))

            line = f"   {color}{icon} {label}{t.normal}"
            parts.append(t.move_xy(0, row_y) + line)

        # Elapsed time
        if self.start_time:
            elapsed = time.time() - self.start_time
            elapsed_str = f"   ⏱ {elapsed:.1f}s"
            ey = y_start + len(phases_ordered) + 2
            if ey < y_start + body_height:
                parts.append(
                    t.move_xy(0, ey)
                    + t.color_rgb(140, 135, 170) + elapsed_str + t.normal
                )

        # ── Separator ──
        for row in range(y_start, y_start + body_height):
            parts.append(
                t.move_xy(phase_width + 1, row)
                + t.color_rgb(60, 58, 70) + "│" + t.normal
            )

        # ── Log panel ──
        log_label = "── LOGS ──"
        parts.append(
            t.move_xy(phase_width + 3, y_start)
            + t.color_rgb(130, 180, 240) + log_label + t.normal
        )

        # Render log entries
        log_area_height = body_height - 1
        visible_logs = list(self.logs)
        if self.scroll_offset > 0:
            visible_logs = visible_logs[:len(visible_logs) - self.scroll_offset]
        visible_logs = visible_logs[-(log_area_height):]

        for i, entry in enumerate(visible_logs):
            row_y = y_start + 1 + i
            if row_y >= y_start + body_height:
                break

            ts = entry["ts"]
            level = entry["level"]
            msg = entry["msg"]

            level_color = {
                "info":    t.color_rgb(160, 158, 150),
                "success": t.color_rgb(80, 220, 150),
                "warning": t.color_rgb(240, 180, 60),
                "error":   t.color_rgb(255, 90, 80),
            }.get(level, t.color_rgb(160, 158, 150))

            phase_short = entry["phase"].value[:8]
            max_msg_len = log_width - 18
            if len(msg) > max_msg_len:
                msg = msg[:max_msg_len - 1] + "…"

            line = (
                f"{t.color_rgb(80, 78, 70)}{ts} "
                f"{t.color_rgb(100, 95, 180)}{phase_short:>8} "
                f"{level_color}{msg}{t.normal}"
            )

            # Clear line first to avoid artifacts
            clear = " " * max(0, log_width)
            parts.append(t.move_xy(phase_width + 3, row_y) + clear)
            parts.append(t.move_xy(phase_width + 3, row_y) + line)

        return parts

    def _render_footer(self) -> list[str]:
        """Render the status/result bar at the bottom."""
        t = self.term
        parts = []
        y = self.h - 3

        # Separator
        parts.append(
            t.move_xy(0, y)
            + t.color_rgb(60, 58, 70) + "  " + "─" * (self.w - 4) + t.normal
        )

        # Result / status line
        if self.report and self.report.success and self.report.dashboard_url:
            result_line = (
                f"  {t.color_rgb(80, 220, 150)}{t.bold}✅ DASHBOARD READY{t.normal}  "
                f"{t.color_rgb(160, 158, 150)}→ {t.normal}"
                f"{t.color_rgb(130, 180, 240)}{t.underline}{self.report.dashboard_url}{t.normal}"
            )
            parts.append(t.move_xy(0, y + 1) + result_line)

            # Chart summary
            succeeded = sum(1 for c in self.report.charts_created if c.success)
            total = len(self.report.charts_created)
            summary = f"  {t.color_rgb(120, 115, 160)}{succeeded}/{total} charts  │  "
            if self.report.errors:
                summary += f"{t.color_rgb(255, 180, 60)}{len(self.report.errors)} warning(s){t.normal}"
            else:
                summary += f"{t.color_rgb(80, 220, 150)}no errors{t.normal}"
            parts.append(t.move_xy(0, y + 2) + summary)

        elif self.report and not self.report.success:
            err_count = len(self.report.errors)
            err_line = (
                f"  {t.color_rgb(255, 90, 80)}{t.bold}⚠ PIPELINE ERRORS ({err_count}){t.normal}  "
            )
            if self.report.errors:
                err_line += f"{t.color_rgb(200, 100, 80)}{self.report.errors[0][:60]}{t.normal}"
            parts.append(t.move_xy(0, y + 1) + err_line)
            parts.append(t.move_xy(0, y + 2) + t.color_rgb(120, 115, 160) + "  Check logs above for details" + t.normal)

        else:
            # Status message
            parts.append(
                t.move_xy(0, y + 1)
                + t.color_rgb(140, 135, 170) + f"  {self.status_message}" + t.normal
            )
            spinner = ""
            if self.is_running:
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                idx = int(time.time() * 8) % len(frames)
                spinner = f"  {t.color_rgb(255, 200, 60)}{frames[idx]} Processing...{t.normal}"
            parts.append(t.move_xy(0, y + 2) + spinner)

        return parts

    def _render_help(self) -> list[str]:
        """Render the help overlay."""
        t = self.term
        parts = [t.home + t.clear]

        help_text = [
            ("SUPERSET MCP AGENT — KEYBOARD SHORTCUTS", True),
            ("", False),
            ("  Enter          Submit query and run pipeline", False),
            ("  q              Quit (when idle)", False),
            ("  ?              Toggle this help screen", False),
            ("  Ctrl+L         Clear logs and results", False),
            ("  ↑/↓            Scroll logs", False),
            ("  Esc            Quit", False),
            ("", False),
            ("PIPELINE PHASES:", True),
            ("  0. Health Check    — ping MCP + Superset", False),
            ("  1. Plan Gen        — LLM parses query → JSON plan", False),
            ("  2. Dataset Disc.   — discover schema via MCP", False),
            ("  3. SQL Validation  — probe queries to verify columns", False),
            ("  4. Chart Creation  — generate_chart with self-correction", False),
            ("  5. Dashboard       — assemble + publish", False),
            ("  6. Report          — structured result summary", False),
            ("", False),
            ("Press any key to return...", False),
        ]

        for i, (line, is_header) in enumerate(help_text):
            y = 3 + i
            if is_header:
                parts.append(t.move_xy(4, y) + t.color_rgb(180, 170, 255) + t.bold + line + t.normal)
            else:
                parts.append(t.move_xy(4, y) + t.color_rgb(160, 158, 150) + line + t.normal)

        return parts

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI escape codes for length calculation."""
        import re
        return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
