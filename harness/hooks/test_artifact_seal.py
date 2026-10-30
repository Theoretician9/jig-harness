#!/usr/bin/env python3
"""Пробы печати файла помощника (artifact_seal.py) и записи скила (skill_log.sh).

ДО кода. Контракт: pretask.yaml (artifact_seal.py, skill_log.sh, Р-2), план —
Task 4. Вход хука — по ЗАПИСАННОМУ входу замера agent_id
(`.tasks/hr.pipeline-cybersecurity-check/zamer-agent-id/post.jsonl`): те же
ключи PostToolUse, а не выдуманные. Живые журналы не трогаются: LOG_DIR и
MODEL_ARTIFACTS_CONF — стендовые.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEAL = HERE / "artifact_seal.py"
SKILL_LOG = HERE / "skill_log.sh"
LIB = next((p for p in (HERE.parent / "lib", HERE.parent / "scripts" / "lib") if p.is_dir()), None)
PAPER = ".tasks/t.seal/безопасность.yaml"
MASKS = (
    "артефакты:\n"
    '  ".tasks/*/безопасность.yaml": { вид: безопасность, кто: помощник, печать: true }\n'
    '  "docs/*": { вид: спека, кто: помощник }\n'
)
# Ключи входа PostToolUse — как в записанном замере.
LIVE_KEYS = ["agent_id", "agent_type", "cwd", "duration_ms", "effort", "hook_event_name",
             "permission_mode", "prompt_id", "session_id", "tool_input", "tool_name",
             "tool_response", "tool_use_id", "transcript_path"]

RESULTS = []
_CLEAN = []


def tmp(prefix="печать-стенд-"):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _CLEAN.append(path)
    return path


class Stand:
    def __init__(self, helper_model="claude-opus-5-5"):
        self.root = tmp()
        self.log = tmp("печать-лог-")
        self.masks = self.root / "masks.yaml"
        self.masks.write_text(MASKS, encoding="utf-8")
        self.session = "sess-1"
        self.transcripts = tmp("печать-транскрипты-")
        self.agent = "aab12cd34ef56"
        helper = self.transcripts / self.session / "subagents" / f"agent-{self.agent}.jsonl"
        helper.parent.mkdir(parents=True)
        helper.write_text(json.dumps({"message": {"model": helper_model}}) + "\n", encoding="utf-8")
        (self.transcripts / "main.jsonl").write_text(
            json.dumps({"message": {"model": "claude-sonnet-5-5"}}) + "\n", encoding="utf-8")

    def write(self, rel, text="code_fingerprint: abc\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def payload(self, rel, tool="Write", agent=True):
        data = {"hook_event_name": "PostToolUse", "tool_name": tool,
                "session_id": self.session, "cwd": str(self.root),
                "transcript_path": str(self.transcripts / "main.jsonl"),
                "tool_input": {"file_path": str(self.root / rel), "content": "x"},
                "tool_response": {"success": True}, "duration_ms": 5,
                "permission_mode": "acceptEdits", "effort": "low",
                "prompt_id": "p", "tool_use_id": "tu"}
        if agent:
            data["agent_id"] = self.agent
            data["agent_type"] = "general-purpose"
        assert set(data) - set(LIVE_KEYS) == set(), "ключ вне записанного замера"
        return data

    def env(self):
        env = dict(os.environ)
        env.update({"LOG_DIR": str(self.log), "MODEL_ARTIFACTS_CONF": str(self.masks),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def run_hook(self, payload):
        if not SEAL.exists():
            raise AssertionError(f"нет модуля {SEAL.name}")
        return subprocess.run([sys.executable, str(SEAL)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(), timeout=60)

    def seals(self):
        path = self.log / "artifact-seals.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def a1():
    stand = Stand()
    path = stand.write(PAPER)
    done = stand.run_hook(stand.payload(PAPER))
    assert done.returncode == 0, done.stderr
    rows = stand.seals()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["file"] == PAPER and row["sha256"] == sha(path), row
    assert row["agent"] == stand.agent and row["источник"] == "хук", row
    assert "opus" in row["model"], row


def a2():
    stand = Stand()
    stand.write(PAPER)
    done = stand.run_hook(stand.payload(PAPER, tool="Edit"))
    assert done.returncode == 0 and len(stand.seals()) == 1, (done.stderr, stand.seals())


def a3():
    stand = Stand()
    stand.write(PAPER)
    stand.run_hook(stand.payload(PAPER, agent=False))
    rows = stand.seals()
    assert len(rows) == 1 and rows[0]["agent"] == "", rows


def a4():
    stand = Stand()
    stand.write("docs/x.md", "текст\n")
    stand.write(".tasks/t.seal/pretask.yaml", "a: 1\n")
    for rel in ("docs/x.md", ".tasks/t.seal/pretask.yaml"):
        done = stand.run_hook(stand.payload(rel))
        assert done.returncode == 0, done.stderr
    assert stand.seals() == [], stand.seals()


def a4b_wrong_tool_is_ignored():
    stand = Stand()
    stand.write(PAPER)
    stand.run_hook(stand.payload(PAPER, tool="Read"))
    assert stand.seals() == [], stand.seals()


def a5():
    stand = Stand()
    stand.write(PAPER)
    (stand.log / "artifact-seals.jsonl").mkdir()          # писать в журнал нельзя
    done = stand.run_hook(stand.payload(PAPER))
    assert done.returncode == 0, (done.returncode, done.stderr)
    failures = (stand.log / "hook_failures.log").read_text(encoding="utf-8")
    assert "artifact_seal" in failures, failures


def a5b_garbage_input():
    stand = Stand()
    done = subprocess.run([sys.executable, str(SEAL)], input="не json", capture_output=True,
                          text=True, env=stand.env(), timeout=60)
    assert done.returncode == 0, done.returncode


# ── skill_log.sh ────────────────────────────────────────────────────────────
def skill_stand(switch_on=True):
    """Копия хука со своими соседями-ловушками: переключение не трогает боевое."""
    root = tmp("печать-скил-")
    scripts = root / "scripts"
    (scripts / "hooks").mkdir(parents=True)
    shutil.copy(SKILL_LOG, scripts / "hooks" / "skill_log.sh")
    shutil.copytree(LIB, scripts / "lib")
    calls = root / "вызовы"
    (scripts / "model-for.sh").write_text("#!/usr/bin/env bash\necho sonnet\n", encoding="utf-8")
    (scripts / "model.sh").write_text(
        '#!/usr/bin/env bash\n[ "${1:-}" = "--только-сессия" ] && '
        f'{{ echo "$*" >> "{calls}"; exit 0; }}\necho "модель сейчас: opus"\n', encoding="utf-8")
    (scripts / "handover-check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for script in scripts.glob("*.sh"):
        script.chmod(0o755)
    conf = root / "models.yaml"
    conf.write_text(f"умолчание: haiku\nпереключать_на_границе_шага: {1 if switch_on else 0}\n",
                    encoding="utf-8")
    return root, calls, conf


def run_skill_hook(root, conf, payload):
    log = root / "лог"
    log.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"LOG_DIR": str(log), "PROJECT_DIR": str(root), "HARNESS_TASK": "t.seal",
                "MODELS_CONF": str(conf), "MODEL_SWITCH_STAMP": str(root / "метка"),
                "MODEL_SWITCH_PAUSE_SEC": "0"})
    done = subprocess.run(["bash", str(root / "scripts" / "hooks" / "skill_log.sh")],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)
    rows = []
    path = log / "skills.jsonl"
    if path.is_file():
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return done, rows


def skill_payload(agent):
    data = {"hook_event_name": "PostToolUse", "tool_name": "Skill", "session_id": "s1",
            "tool_input": {"skill": "security-check"},
            "tool_response": {"success": True, "commandName": "security-check"},
            "cwd": "/x", "duration_ms": 3}
    if agent:
        data["agent_id"] = agent
        data["agent_type"] = "general-purpose"
    return data


def a6():
    root, calls, conf = skill_stand()
    done, rows = run_skill_hook(root, conf, skill_payload("aab12cd34ef56"))
    assert done.returncode == 0, done.stderr
    assert len(rows) == 1 and rows[0]["skill"] == "security-check", rows
    assert rows[0].get("агент") == "aab12cd34ef56", rows
    assert not calls.exists() or not calls.read_text(encoding="utf-8").strip(), \
        "помощник, позвавший скил, переключил модель СМЕНЫ"


def a7():
    root, calls, conf = skill_stand()
    done, rows = run_skill_hook(root, conf, skill_payload(None))
    assert len(rows) == 1 and rows[0].get("агент") == "", rows
    assert calls.exists() and calls.read_text(encoding="utf-8").strip(), \
        "скил смены не вызвал переключение модели"


def run_case(name, func):
    try:
        func()
        RESULTS.append((name, True))
        print(f"  ок    {name}")
    except Exception as err:        # noqa: BLE001
        RESULTS.append((name, False))
        print(f"  ПЛОХО {name}: {err or err.__class__.__name__}")


def main():
    for name, func in [
        ("A1 Write помощника по маске — строка с agent, model, sha256", a1),
        ("A2 Edit помощника — строка", a2),
        ("A3 Write без agent_id — строка с agent пустым", a3),
        ("A4 путь вне маски печати — строки нет", a4),
        ("A4b чужой инструмент (Read) — строки нет", a4b_wrong_tool_is_ignored),
        ("A5 журнал недоступен — код 0 и строка в hook_failures.log", a5),
        ("A5b мусор на входе — код 0", a5b_garbage_input),
        ("A6 вход skill_log со agent_id: поле «агент», модель смены НЕ переключается", a6),
        ("A7 вход skill_log без agent_id: переключение зовётся", a7),
    ]:
        run_case(name, func)
    for path in _CLEAN:
        shutil.rmtree(path, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"ПЕЧАТЬ: случаев {len(RESULTS)}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
