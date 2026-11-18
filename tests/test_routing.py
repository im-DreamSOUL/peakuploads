"""Each kind of job (finding clips, captions, titles and hooks) can use its own AI service and model. Keys are kept per
service, free-tier daily limits are respected, and a service that says "too many requests" is left for a while.
Nothing leaves this machine: the network is replaced."""
import json
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-route-")
os.environ["DATABASE_URL"] = ""
os.environ.update(PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="gem-a, gem-b", PEAKCUT_AI_KEY="gemkey-1234567890",
                  PEAKCUT_AI_BASE_URL="")
sys.path.insert(0, ".")
from studio import ai, brain, db, usage  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


class Reply:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


CLIP = {"start": 1, "end": 20, "score": 80, "hook": "WOW", "title": "t", "description": "d", "hashtags": ["a"],
        "reason": "r"}
ANSWER = json.dumps([CLIP])
calls = []
BAD = {"groq": False}


def fake_post(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    calls.append(url)
    if "groq" in url and BAD["groq"]:
        return Reply(429, {"error": {"message": "rate limit reached"}})
    if url.endswith(":generateContent"):
        return Reply(200, {"candidates": [{"content": {"parts": [{"text": ANSWER}]}}],
                           "usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 130}})
    if url.endswith("/chat/completions"):
        return Reply(200, {"choices": [{"message": {"content": ANSWER}}],
                           "usage": {"prompt_tokens": 90, "completion_tokens": 20}})
    return Reply(404, {})


ai.httpx.post = fake_post

# ---- nothing set for the jobs: the main service and its models, in the order given, as before
plan = ai.targets_for("clip")
check("with nothing set, a job uses the main models in order",
      [t["models"][0] for t in plan] == ["gem-a", "gem-b"] and all(t["provider"] == "gemini" for t in plan), str(plan))

# ---- keys are kept per service: switching the main service does not lose the others
ai.save("groq", "llama-x", "", new_key="groqkey-1234567890")
ai.save("gemini", "gem-a, gem-b", "", None)
check("saving another service keeps the first one's key", ai.key_for("groq") == "groqkey-1234567890")
check("the main service is back to gemini", ai.provider() == "gemini")

# ---- one job on its own service
ai.save_tasks({"title": {"mode": "custom", "provider": "groq", "model": "llama-x"}})
check("a job set to a service goes there first", ai.targets_for("title")[0]["provider"] == "groq")
check("the other jobs keep the main service", ai.targets_for("clip")[0]["provider"] == "gemini")
check("the chosen service's models are followed by the main ones",
      [t["provider"] for t in ai.targets_for("title")] == ["groq", "gemini", "gemini"])

try:
    ai.save_tasks({"caption": {"mode": "custom", "provider": "openrouter", "model": "x/y"}})
    check("a service without a saved key is refused", False)
except ValueError as e:
    check("a service without a saved key is refused", "Save a key" in str(e), str(e))

try:
    ai.save_tasks({"clip": {"mode": "sometimes"}})
    check("an unknown mode is refused", False)
except ValueError:
    check("an unknown mode is refused", True)

# ---- automatic: only services with keys, never one that has used up its daily limit
ai.save_tasks({"clip": {"mode": "auto"}})
auto_providers = {t["provider"] for t in ai.targets_for("clip")}
check("automatic uses only services that have keys", auto_providers <= {"gemini", "groq"} and auto_providers,
      str(auto_providers))
usage.today_by_model = lambda: {("gemini", "gemini-2.5-flash"): (50, 0)}
ai._catalog_cache["rows"] = [
    {"provider": "gemini", "model": "gemini-2.5-flash", "tier": "free", "speed": 5, "quality": 5, "daily_calls": 50},
    {"provider": "groq", "model": "llama-x", "tier": "free", "speed": 4, "quality": 3, "daily_calls": 0},
]
names = [t["models"][0] for t in ai.targets_for("clip")]
check("a model at its daily limit is left out", "gemini-2.5-flash" not in names and "llama-x" in names, str(names))

# ---- a service that said "too many requests" goes last
ai._cool.clear()
ai.cool("groq")
check("a service that said too many requests is paused", ai.cooling_for("groq") > 0)
ai.save_tasks({"title": {"mode": "custom", "provider": "groq", "model": "llama-x"}})

# ---- the whole path: a rate-limited service is passed over, and the answer still comes back
brain.time.sleep = lambda s: None
ai._cool.clear()
BAD["groq"] = True
calls.clear()
out, model, spent = brain.ask_ai("Pick clips.", purpose="Video: title and hook")
check("a rate-limited service is passed over", model == "gem-a" and out and out[0]["hook"] == "WOW", str(model))
check("the rate-limited service is paused after it keeps refusing", ai.cooling_for("groq") > 0)
check("it was tried, then the main service answered", calls[0].count("groq") >= 1 and "generateContent" in calls[-1],
      str(calls))
BAD["groq"] = False
ai.save_tasks({})

# ---- which job a call belongs to, from its purpose
check("captions", brain._job("Caption wording fix") == "caption")
check("clip finding", brain._job("Video: finding clips") == "clip")
check("titles and hooks", brain._job("Video: title and hook") == "title")
check("hand clips are titles", brain._job("Hand clip: title and hook") == "title")

# ---- the limits shown on the usage page
lim = usage.limits()
labels = {s["provider"]: s for s in lim}
check("the usage page lists each service with a key", "gemini" in labels and "groq" in labels, str(lim))
check("a model with a daily limit shows it", any(m["cap"] == 50 and m["calls_today"] == 50
                                              for m in labels["gemini"]["models"]), str(labels.get("gemini")))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
ai._cool.clear()
db.engine.dispose()
import shutil  # noqa: E402
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
