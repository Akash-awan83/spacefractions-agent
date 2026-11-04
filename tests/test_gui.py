"""GUI tests: the real Tk window is driven programmatically with a fake model.
Skipped automatically when Tkinter or a display is not available."""
import gc
import time

import pytest

tk = pytest.importorskip("tkinter")

from agent.gui import AgentApp  # noqa: E402
from test_loop import MINI_PROJECT, FakeModel, call  # noqa: E402


@pytest.fixture(scope="module")
def tk_root():
    """ONE Tk interpreter for all GUI tests. Creating many Tk() instances in one process
    fails intermittently on Windows (Tcl init race), so each test gets a Toplevel instead."""
    last = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except tk.TclError as e:
            last = e
            time.sleep(0.5)
    else:
        pytest.skip(f"no display available: {last}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path, monkeypatch, tk_root):
    for env in ("DEEPSEEK_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    win = tk.Toplevel(tk_root)
    win.withdraw()
    a = AgentApp(win, cfg_path=tmp_path / "cfg.json")
    a.msgs = []
    a._message = lambda kind, title, text: (a.msgs.append((kind, text)), True)[1]
    a.out_var.set(str(tmp_path / "out"))
    yield a
    if a.agent:
        a.agent.cancel()
    for t in (a.worker, a._ping_thread):
        if t:
            t.join(10)
    win.update()
    win.destroy()
    # Collect the old window's Tk objects HERE, on the main thread; otherwise Python's
    # cyclic GC may free them later inside the next test's worker thread.
    del a, win
    gc.collect()


def pump(app, until, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if until():
            return True
        time.sleep(0.02)
    return False


def log_text(app):
    return app.log.get("1.0", "end")


def test_defaults_prefill_sample_inputs(app):
    assert app.doc_var.get().endswith("Architecture_Documentation.md")
    assert app.views_var.get().endswith("Architecture_View.md")
    assert app.model_var.get() == "deepseek-v4-pro" and app.budget_var.get() == "0.90"


def test_run_requires_key(app):
    app.start_run(confirm=False)
    assert app.msgs and "API key" in app.msgs[-1][1]


def test_save_settings_persists_key(app, tmp_path):
    app.key_var.set("sk-abcdef123456")
    assert app.save_settings()
    assert "sk-abcdef123456" in (tmp_path / "cfg.json").read_text()
    assert "sk-...3456" in app.key_status.cget("text")


def test_check_inputs_lists_conflicts(app):
    app.check_inputs()
    assert "20 conflicts" in log_text(app)


def test_test_key_with_fake_model(app):
    app.key_var.set("sk-fake-123456")
    app.llm_override = FakeModel([{"role": "assistant", "content": "pong"}])
    app.test_key()
    assert pump(app, lambda: "Key works" in log_text(app))


def test_full_run_in_the_window(app, tmp_path):
    from pathlib import Path
    from agent.preprocess import build
    from fake_projects import script_writing, valid_files
    root = Path(__file__).resolve().parents[1]
    arch = build(root / "inputs/Architecture_Documentation.md", root / "inputs/Architecture_View.md")
    app.llm_override = FakeModel(MINI_PROJECT[:1] + script_writing(valid_files(arch)))
    app.start_run(confirm=False)
    assert pump(app, lambda: app.status_lbl.cget("text") == "Completed", timeout=60)
    assert "write code" in app.plan.get("1.0", "end")
    assert app.cost_lbl.cget("text").startswith("$") and app.cost_bar["value"] > 0
    assert "PASSED" in log_text(app)
    assert (tmp_path / "out" / "spacefractions" / "game" / "__init__.py").is_file()
    assert str(app.run_btn.cget("state")) == "normal"


def test_stop_button_cancels(app):
    looping = [{"role": "assistant", "content": None, "tool_calls": [call("list_dir", {}, f"x{i}")]}
               for i in range(500)]

    class SlowModel(FakeModel):
        def chat(self, *a, **k):
            time.sleep(0.05)
            return super().chat(*a, **k)

    app.llm_override = SlowModel(looping)
    app.cfg["max_steps"] = 500
    app.steps_var.set("500")
    app.start_run(confirm=False)
    assert pump(app, lambda: "step 2" in app.info_lbl.cget("text"))
    app.stop_run()
    assert pump(app, lambda: app.status_lbl.cget("text") == "Stopped by user")


def test_provider_switch_keeps_keys_separate(app):
    from agent.config import PROVIDERS, resolve_api_key
    app.key_var.set("sk-deep-1234")
    app.provider_var.set(PROVIDERS["gemini"]["label"])
    app._on_provider_change()
    assert app.model_var.get() == "gemini-3.8-flash" and app.key_var.get() == ""
    assert "TESTING ONLY" in app.provider_note.cget("text")
    app.key_var.set("AIza-gem-5678")
    assert app.save_settings(quiet=True)
    assert resolve_api_key(app.cfg, "gemini") == "AIza-gem-5678"
    assert resolve_api_key(app.cfg, "deepseek") == "sk-deep-1234"
    app.provider_var.set(PROVIDERS["deepseek"]["label"])
    app._on_provider_change()
    assert app.key_var.get() == "sk-deep-1234" and app.provider_note.cget("text") == ""


def test_peak_hours_ask_before_a_deepseek_run(app, monkeypatch):
    import agent.gui as gui
    monkeypatch.setattr(gui, "is_peak", lambda pricing: True)
    asked = []
    app._message = lambda kind, title, text: (asked.append(text), False)[1]   # user answers "No"
    app.key_var.set("sk-deep-1234")
    app.start_run(confirm=True)
    assert asked and "PEAK" in asked[-1] and app.worker is None
