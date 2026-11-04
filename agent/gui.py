"""Desktop window for the code agent (Tkinter: ships with Python, so the .exe needs nothing else).

Sections, matching the task brief:
  1. DeepSeek API key configuration (hidden entry, save, test)
  2. Input file selection (architecture documentation + views) and output directory
  3. Task triggering (Check inputs, Run agent, Stop) with a live plan, log and cost meter
  4. Running the generated game

Threading: the agent runs in a background thread and never touches widgets. It
puts events on a queue; the Tk thread drains the queue every 100 ms (_poll).
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .config import (PROVIDERS, config_dir, load_config, make_client, mask_key, provider_of, resolve_api_key,
                     resolve_model, resolve_pricing, save_config, set_api_key, set_model)
from .events import format_event
from .llm import PEAK_WARNING, CostTracker, LLMError, is_peak
from .pipeline import create_agent, prepare, task_for

APP_TITLE = "SpaceFractions Code Agent"
STATUS_TEXT = {"completed": "Completed", "completed_with_issues": "Finished with open issues", "budget_exhausted": "Stopped: budget reached",
               "max_steps": "Stopped: step limit reached", "cancelled": "Stopped by user", "error": "Error"}


def resource_path(rel: str) -> Path:
    """Files shipped with the app (sample inputs). Inside the .exe they live in sys._MEIPASS."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def sample_inputs() -> tuple[Path, Path]:
    """The bundled example inputs. Inside the .exe they sit in a temporary folder that
    disappears on exit, so they are copied once to a permanent place in the user profile."""
    names = ("Architecture_Documentation.md", "Architecture_View.md")
    src = [resource_path(f"inputs/{n}") for n in names]
    if not getattr(sys, "frozen", False):
        return src[0], src[1]
    dest_dir = config_dir() / "sample_inputs"
    dest = [dest_dir / n for n in names]
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for s_, d in zip(src, dest):
            if s_.is_file() and not d.is_file():
                shutil.copyfile(s_, d)
        return dest[0], dest[1]
    except OSError:
        return src[0], src[1]


def default_output() -> Path:
    desktop = Path.home() / "Desktop"
    return (desktop if desktop.is_dir() else Path.home()) / "SpaceFractions_Output"


class AgentApp:
    def __init__(self, root: tk.Tk, cfg_path: Path | None = None):
        self.root = root
        self.cfg_path = cfg_path
        self.cfg = load_config(cfg_path)
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.agent = None
        self.llm_override = None          # tests inject a fake model here
        self._ping_thread: threading.Thread | None = None
        self.files_written: set[str] = set()
        self._build()
        self._fill_defaults()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    # ------------------------------------------------------------------ layout
    def _build(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry("1080x760")
        r.minsize(900, 620)
        style = ttk.Style(r)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Sub.TLabel", foreground="#555")
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))

        top = ttk.Frame(r, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="MAAD architecture documents  ->  verified desktop code, powered by DeepSeek",
                  style="Sub.TLabel").pack(anchor="w")

        # 1. API settings
        api = ttk.LabelFrame(r, text=" 1. DeepSeek API ", padding=8)
        api.pack(fill="x", padx=12, pady=4)
        ttk.Label(api, text="Provider").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.provider_var = tk.StringVar()
        self._labels = {v["label"]: k for k, v in PROVIDERS.items()}
        self.provider_box = ttk.Combobox(api, textvariable=self.provider_var, state="readonly", width=40,
                                         values=[v["label"] for v in PROVIDERS.values()])
        self.provider_box.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        self.provider_box.bind("<<ComboboxSelected>>", self._on_provider_change)
        self.provider_note = ttk.Label(api, text="", foreground="#e65100")
        self.provider_note.grid(row=2, column=1, columnspan=5, sticky="e", pady=(6, 0))
        self._current_provider = "deepseek"
        ttk.Label(api, text="API key").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(api, textvariable=self.key_var, show="\u2022", width=52)
        self.key_entry.grid(row=0, column=1, sticky="we", padx=6)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(api, text="show", variable=self.show_key,
                        command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "\u2022")
                        ).grid(row=0, column=2)
        ttk.Button(api, text="Save", command=self.save_settings).grid(row=0, column=3, padx=3)
        self.test_btn = ttk.Button(api, text="Test key", command=self.test_key)
        self.test_btn.grid(row=0, column=4, padx=3)
        self.key_status = ttk.Label(api, text="", style="Sub.TLabel")
        self.key_status.grid(row=0, column=5, sticky="w", padx=6)

        ttk.Label(api, text="Model").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model_var = tk.StringVar()
        ttk.Entry(api, textvariable=self.model_var, width=22).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        lim = ttk.Frame(api)
        lim.grid(row=1, column=1, columnspan=5, sticky="e", pady=(6, 0))
        ttk.Label(lim, text="Budget $").pack(side="left")
        self.budget_var = tk.StringVar()
        ttk.Spinbox(lim, from_=0.05, to=1.0, increment=0.05, textvariable=self.budget_var, width=6).pack(side="left", padx=(2, 12))
        ttk.Label(lim, text="Max steps").pack(side="left")
        self.steps_var = tk.StringVar()
        ttk.Spinbox(lim, from_=5, to=200, increment=5, textvariable=self.steps_var, width=6).pack(side="left", padx=2)
        api.columnconfigure(1, weight=1)

        # 2. Inputs / output
        io = ttk.LabelFrame(r, text=" 2. Inputs and output ", padding=8)
        io.pack(fill="x", padx=12, pady=4)
        self.doc_var, self.views_var, self.out_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        rows = [("Architecture documentation (.md)", self.doc_var, self._pick_doc),
                ("Architecture views / UML (.md)", self.views_var, self._pick_views),
                ("Output folder", self.out_var, self._pick_out)]
        for i, (label, var, cmd) in enumerate(rows):
            ttk.Label(io, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(io, textvariable=var).grid(row=i, column=1, sticky="we", padx=6, pady=2)
            ttk.Button(io, text="Browse...", command=cmd).grid(row=i, column=2, pady=2)
        ttk.Button(io, text="Open folder", command=self.open_output).grid(row=2, column=3, padx=3)
        io.columnconfigure(1, weight=1)

        # 3. Actions
        act = ttk.Frame(r, padding=(12, 4))
        act.pack(fill="x")
        self.check_btn = ttk.Button(act, text="Check inputs", command=self.check_inputs)
        self.check_btn.pack(side="left")
        self.run_btn = ttk.Button(act, text="\u25B6  Run agent", command=self.start_run)
        self.run_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(act, text="\u25A0  Stop", command=self.stop_run, state="disabled")
        self.stop_btn.pack(side="left")
        self.game_btn = ttk.Button(act, text="Play generated game", command=self.run_game)
        self.game_btn.pack(side="right")

        # status bar: status, step, cost meter
        st = ttk.Frame(r, padding=(12, 2))
        st.pack(fill="x")
        self.status_lbl = ttk.Label(st, text="Ready", style="Big.TLabel")
        self.status_lbl.pack(side="left")
        self.cost_lbl = ttk.Label(st, text="$0.0000")
        self.cost_lbl.pack(side="right")
        self.cost_bar = ttk.Progressbar(st, length=220, maximum=100)
        self.cost_bar.pack(side="right", padx=8)
        ttk.Label(st, text="Cost").pack(side="right")
        self.info_lbl = ttk.Label(st, text="", style="Sub.TLabel")
        self.info_lbl.pack(side="left", padx=16)

        # plan + log
        panes = ttk.PanedWindow(r, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        left = ttk.LabelFrame(panes, text=" Agent plan ", padding=4)
        self.plan = tk.Text(left, width=36, wrap="word", height=10, state="disabled", relief="flat")
        self.plan.pack(fill="both", expand=True)
        self.plan.tag_configure("done", foreground="#2e7d32")
        self.plan.tag_configure("in_progress", foreground="#1565c0", font=("Segoe UI", 9, "bold"))
        panes.add(left, weight=1)
        right = ttk.LabelFrame(panes, text=" Activity log ", padding=4)
        self.log = ScrolledText(right, wrap="word", height=10, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#c62828")
        self.log.tag_configure("step", foreground="#1565c0")
        self.log.tag_configure("done", foreground="#2e7d32", font=("Consolas", 9, "bold"))
        panes.add(right, weight=3)

    def _fill_defaults(self):
        c = self.cfg
        self._current_provider = provider_of(c)
        self.provider_var.set(PROVIDERS[self._current_provider]["label"])
        self.key_var.set(resolve_api_key(c))
        self.model_var.set(resolve_model(c))
        self._update_provider_note()
        self.budget_var.set(f"{float(c['budget_usd']):.2f}")
        self.steps_var.set(str(c["max_steps"]))
        sample_doc, sample_views = sample_inputs()
        self.doc_var.set(c["last_doc"] if c["last_doc"] and Path(c["last_doc"]).is_file()
                         else (str(sample_doc) if sample_doc.is_file() else ""))
        self.views_var.set(c["last_views"] if c["last_views"] and Path(c["last_views"]).is_file()
                           else (str(sample_views) if sample_views.is_file() else ""))
        self.out_var.set(c["last_output"] or str(default_output()))
        self._update_key_status()

    # ------------------------------------------------------------------ helpers
    def _message(self, kind: str, title: str, text: str) -> bool:
        """All dialogs go through here (tests replace it)."""
        if kind == "ask":
            return messagebox.askyesno(title, text, parent=self.root)
        getattr(messagebox, {"error": "showerror", "warn": "showwarning"}.get(kind, "showinfo"))(
            title, text, parent=self.root)
        return True

    def _log(self, line: str, tag: str | None = None):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_plan(self, items: list):
        self.plan.configure(state="normal")
        self.plan.delete("1.0", "end")
        mark = {"done": "\u2714", "in_progress": "\u25B6", "pending": "\u2022"}
        for it in items:
            self.plan.insert("end", f"{mark.get(it['status'], '-')} {it['content']}\n", it["status"])
        self.plan.configure(state="disabled")

    def _selected_provider(self) -> str:
        return self._labels.get(self.provider_var.get(), "deepseek")

    def _update_provider_note(self):
        p = self._current_provider
        self.provider_note.configure(
            text="" if p == "deepseek" else "TESTING ONLY - the final run and the demo must use DeepSeek")

    def _on_provider_change(self, _event=None):
        new = self._selected_provider()
        if new == self._current_provider:
            return
        # keep what was typed for the previous provider, then load the new provider's values
        set_api_key(self.cfg, self.key_var.get().strip(), self._current_provider)
        set_model(self.cfg, self.model_var.get().strip(), self._current_provider)
        self._current_provider = new
        self.cfg["provider"] = new
        self.key_var.set(resolve_api_key(self.cfg, new))
        self.model_var.set(resolve_model(self.cfg, new))
        self._update_key_status()
        self._update_provider_note()
        self._log(f"Provider: {PROVIDERS[new]['label']}")

    def _update_key_status(self):
        self.key_status.configure(text=f"current: {mask_key(self.key_var.get().strip())}")

    def _read_settings(self) -> dict:
        cfg = dict(self.cfg)
        p = self._current_provider
        cfg["provider"] = p
        set_api_key(cfg, self.key_var.get().strip(), p)
        set_model(cfg, self.model_var.get().strip(), p)
        try:
            cfg["budget_usd"] = min(1.0, max(0.01, float(self.budget_var.get())))
            cfg["max_steps"] = max(1, int(float(self.steps_var.get())))
        except ValueError:
            raise ValueError("Budget and max steps must be numbers.")
        cfg["last_doc"], cfg["last_views"], cfg["last_output"] = (
            self.doc_var.get().strip(), self.views_var.get().strip(), self.out_var.get().strip())
        return cfg

    def save_settings(self, quiet: bool = False) -> bool:
        try:
            self.cfg = self._read_settings()
        except ValueError as e:
            self._message("error", APP_TITLE, str(e))
            return False
        save_config(self.cfg, self.cfg_path)
        self._update_key_status()
        if not quiet:
            self._log("Settings saved (the key is stored in your user profile, not in the project).")
        return True

    def _pick_doc(self):
        p = filedialog.askopenfilename(title="Architecture documentation", filetypes=[("Markdown", "*.md"), ("All", "*.*")])
        if p:
            self.doc_var.set(p)

    def _pick_views(self):
        p = filedialog.askopenfilename(title="Architecture views", filetypes=[("Markdown", "*.md"), ("All", "*.*")])
        if p:
            self.views_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(title="Output folder")
        if p:
            self.out_var.set(p)

    def open_output(self):
        out = Path(self.out_var.get().strip())
        if not out.is_dir():
            self._message("info", APP_TITLE, "The output folder does not exist yet.")
            return
        if os.name == "nt":
            os.startfile(out)  # noqa: S606 - opening Explorer on a user-chosen folder
        else:
            subprocess.Popen(["xdg-open", str(out)])

    def _busy(self, running: bool):
        for b in (self.run_btn, self.check_btn, self.test_btn, self.game_btn):
            b.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------------ actions
    def test_key(self):
        if not self.save_settings(quiet=True):
            return
        if not resolve_api_key(self.cfg):
            self._message("error", APP_TITLE, "Enter the API key first.")
            return
        self.test_btn.configure(state="disabled")
        self._log("Testing the API key with one tiny request ...")
        cfg = dict(self.cfg)

        def work():
            try:
                client = self.llm_override or make_client(cfg, timeout=120, max_retries=1)
                msg, usage, _ = client.chat([{"role": "user", "content": "Reply with exactly: pong"}],
                                            thinking=False, max_tokens=10)
                cost = CostTracker(resolve_pricing(cfg)).add(usage)["cost_usd"]
                self.events.put(("ping", {"ok": True, "text": f"Key works. Reply: {msg.get('content')!r}, cost ${cost:.6f}"}))
            except LLMError as e:
                self.events.put(("ping", {"ok": False, "text": str(e)}))

        self._ping_thread = threading.Thread(target=work, daemon=True)
        self._ping_thread.start()

    def _validate_inputs(self) -> tuple[Path, Path, Path] | None:
        doc, views, out = (Path(v.get().strip()) for v in (self.doc_var, self.views_var, self.out_var))
        if not doc.is_file():
            self._message("error", APP_TITLE, "Select the architecture documentation file (.md).")
            return None
        if not views.is_file():
            self._message("error", APP_TITLE, "Select the architecture views file (.md).")
            return None
        if not str(out) or str(out) == ".":
            self._message("error", APP_TITLE, "Choose an output folder.")
            return None
        return doc, views, out

    def check_inputs(self):
        paths = self._validate_inputs()
        if not paths:
            return
        try:
            arch = prepare(*paths)
        except Exception as e:  # noqa: BLE001 - show any parse failure to the user
            self._message("error", APP_TITLE, f"Could not parse the inputs:\n{e}")
            return
        sev = {s: sum(c["severity"] == s for c in arch["conflicts"]) for s in ("high", "medium", "low")}
        self._log(f"[check] {arch['project']['name']}: {len(arch['requirements'])} requirements, "
                  f"{arch['views']['count']} diagrams, {len(arch['conflicts'])} conflicts "
                  f"(high {sev['high']}, medium {sev['medium']}, low {sev['low']})", "step")
        for c in arch["conflicts"]:
            self._log(f"   {c['id']} [{c['severity']}] {c['finding'][:150]}")
        self._log(f"   saved to {paths[2] / '.agent' / 'architecture.json'}")

    def start_run(self, confirm: bool = True):
        if self.worker and self.worker.is_alive():
            return
        if not self.save_settings(quiet=True):
            return
        if not resolve_api_key(self.cfg) and self.llm_override is None:
            self._message("error", APP_TITLE, "Enter and save your API key first.")
            return
        if not resolve_model(self.cfg) and self.llm_override is None:
            self._message("error", APP_TITLE, "Enter a model id for this provider.")
            return
        paths = self._validate_inputs()
        if not paths:
            return
        out = paths[2]
        if confirm and out.is_dir() and any(p.name != ".agent" for p in out.iterdir()):
            if not self._message("ask", APP_TITLE, f"{out}\nalready contains files.\n\nYes = continue (resume) the "
                                                   f"earlier run in this folder.\nNo = cancel and choose an empty folder."):
                return
        if confirm and provider_of(self.cfg) == "deepseek" and is_peak(resolve_pricing(self.cfg)):
            if not self._message("ask", APP_TITLE, PEAK_WARNING + "\n\nStart anyway at double price?"):
                return
        self.files_written.clear()
        self._set_plan([])
        self.cost_bar["value"] = 0
        self._busy(True)
        self.status_lbl.configure(text="Running ...")
        p = provider_of(self.cfg)
        if p != "deepseek":
            self._log("NOTE: free testing provider - this run does NOT count as the DeepSeek run.", "err")
        self._log(f"=== Run: {p}:{resolve_model(self.cfg)} | budget ${self.cfg['budget_usd']:.2f} | "
                  f"max {self.cfg['max_steps']} steps | output {out}", "step")
        cfg = dict(self.cfg)
        self.worker = threading.Thread(target=self._work, args=(cfg, *paths), daemon=True)
        self.worker.start()

    def _work(self, cfg, doc, views, out):
        try:
            task = task_for(out)
            if "RESUME:" in task:
                self.events.put(("note", {"text": "Existing files found - the agent will continue the earlier run."}))
            arch = prepare(doc, views, out)
            self.events.put(("preprocessed", {"conflicts": len(arch["conflicts"]), "diagrams": arch["views"]["count"]}))
            self.agent = create_agent(cfg, arch, out, on_event=lambda k, d: self.events.put((k, d)),
                                      llm=self.llm_override)
            result = self.agent.run(task)
            self.events.put(("result", {"result": result}))
        except Exception as e:  # noqa: BLE001 - report everything, never crash the window
            self.events.put(("fatal", {"message": f"{type(e).__name__}: {e}"}))

    def stop_run(self):
        if self.agent:
            self.agent.cancel()
            self.status_lbl.configure(text="Stopping after the current step ...")
            self._log("Stop requested - the agent stops after the current model call.", "err")

    def run_game(self):
        main_py = Path(self.out_var.get().strip()) / "main.py"
        if not main_py.is_file():
            self._message("info", APP_TITLE, "No main.py in the output folder yet. Run the agent first.")
            return
        cmd = ([sys.executable, "--run-script", str(main_py)] if getattr(sys, "frozen", False)
               else [sys.executable, str(main_py)])
        subprocess.Popen(cmd, cwd=str(main_py.parent))
        self._log(f"Started the generated game: {main_py}")

    # ------------------------------------------------------------------ event pump
    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, kind: str, data: dict):
        if kind == "note":
            self._log(data["text"], "step")
            return
        if kind == "ping":
            self.test_btn.configure(state="normal")
            self._log(data["text"], None if data["ok"] else "err")
            self._message("info" if data["ok"] else "error", APP_TITLE, data["text"])
            return
        if kind == "todo":
            self._set_plan(data["items"])
            return
        if kind == "file":
            self.files_written.add(data["path"])
        if kind == "usage":
            budget = float(self.cfg["budget_usd"])
            self.cost_lbl.configure(text=f"${data['total_usd']:.4f} / ${budget:.2f}")
            self.cost_bar["value"] = min(100, 100 * data["total_usd"] / budget)
            self.info_lbl.configure(text=f"step {data['step']}  |  cache hit {data['cache_hit_rate']:.0%}"
                                         f"  |  files {len(self.files_written)}")
        if kind == "result":
            res = data["result"]
            self._busy(False)
            self.status_lbl.configure(text=STATUS_TEXT.get(res.status, res.status))
            c = res.cost
            self._log(f"=== {STATUS_TEXT.get(res.status, res.status)}: {res.steps} steps, {res.seconds}s, "
                      f"{len(res.files_written)} files, cost ${c['cost_usd']:.4f} "
                      f"(cache hit {c['cache_hit_rate']:.0%})", "done" if res.status == "completed" else "err")
            if res.error:
                self._log(f"   {res.error}", "err")
            if res.final_message:
                self._log("   " + res.final_message.strip()[:600])
            return
        if kind == "fatal":
            self._busy(False)
            self.status_lbl.configure(text="Error")
            self._log("FATAL: " + data["message"], "err")
            self._message("error", APP_TITLE, data["message"])
            return
        line = format_event(kind, data)
        if line:
            if kind == "verify":
                self._log(line, "done" if data["passed"] else "err")
                return
            tag = "err" if kind == "error" or (kind == "tool_result" and not data.get("ok")) else (
                "step" if kind in ("thinking", "preprocessed") else "done" if kind == "done" else None)
            self._log(line, tag)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not self._message("ask", APP_TITLE, "The agent is still running. Stop it and quit?"):
                return
            if self.agent:
                self.agent.cancel()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    AgentApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
