"""Forward-log view: join, metrics, and the local/GitHub loaders.

GitHub is faked; nothing touches the network.
"""

import json
from pathlib import Path

import pytest

from jevtrade.forward.log import ForwardLog, NotFound, join, metrics, parse_jsonl
from jevtrade.forward.settings import ForwardLogConfig

FIXTURE = Path(__file__).parent / "fixtures" / "forward"


def load(folder):
    return [r for p in sorted((FIXTURE / folder).glob("*.jsonl")) for r in parse_jsonl(p.read_text())]


@pytest.fixture
def rows():
    return join(load("decisions"), load("outcomes"))


def by(rows, sym, i):
    ts = 1791086400000 + i * 3_600_000
    return next(r for r in rows if r["symbol"] == sym and r["candle_ts"] == ts)


def test_parse_drops_state_and_bad_lines():
    recs = load("decisions")
    assert len(recs) == 8   # the "not json" line is skipped
    assert all("state" not in r and "raw_response" not in r for r in recs)


def test_join_one_row_per_candle_newest_first(rows):
    assert len(rows) == 7
    assert [r["candle_ts"] for r in rows] == sorted((r["candle_ts"] for r in rows), reverse=True)
    # The retried error is replaced by its answer; the unretried one stays an error.
    assert by(rows, "ETH/USD", 1)["status"] == "answered"
    assert by(rows, "ETH/USD", 2)["status"] == "error"


def test_join_hits_pending_abstain(rows):
    hit = by(rows, "BTC/USD", 0)
    assert hit["predicted"] == "flat" and hit["outcome"]["direction"] == "flat" and hit["hit"] is True
    assert hit["confidence"] == 0.8 and hit["regime"] == "range" and hit["p_adverse"] == 0.1
    assert by(rows, "BTC/USD", 1)["hit"] is False
    pending = rows[0]
    assert pending["status"] == "answered" and pending["outcome"] is None and pending["hit"] is None
    abstain = by(rows, "BTC/USD", 2)
    assert abstain["outcome"] is not None and abstain["predicted"] is None and abstain["hit"] is None
    err = by(rows, "ETH/USD", 2)
    assert err["outcome"] is None and err["hit"] is None and err["predicted"] is None


def test_metrics(rows):
    m = metrics(rows, cost_usd=1.5)
    assert (m["decisions"], m["answered"], m["abstain"], m["error"]) == (7, 5, 1, 1)
    assert (m["scored"], m["pending"], m["hits"]) == (4, 1, 2)
    assert m["oldest_pending_ts"] == rows[0]["candle_ts"]
    assert m["hit_rate"] == 0.5
    assert m["realized"] == {"up": 1, "flat": 2, "down": 1}
    assert m["baselines"]["majority"] == {"label": "flat", "hit_rate": 0.5}
    assert m["baselines"]["always_flat"]["hit_rate"] == 0.5
    # rows: predicted up/flat/down; columns: realized up/flat/down
    assert m["confusion"]["matrix"] == [[0, 1, 0], [1, 1, 0], [0, 0, 1]]
    cal = {(b["lo"], b["hi"]): b for b in m["calibration"]}
    assert sum(b["n"] for b in m["calibration"]) == 4
    assert cal[(0.5, 0.6)]["hit_rate"] == 1.0 and cal[(0.6, 0.7)]["hit_rate"] == 0.0
    assert cal[(0.9, 1.0)]["n"] == 1 and cal[(0.0, 0.5)]["hit_rate"] is None
    d = m["direction_scores"]
    assert d["n"] == 4 and 0 < d["brier"] < 2 and d["log_loss"] > 0 and d["brier_base_rate"] > 0
    a = m["adverse_move_scores"]
    assert a["n"] == 4 and a["base_rate"] == 0.25
    assert a["brier"] == pytest.approx((0.1**2 + 0.2**2 + 0.3**2 + 0.05**2) / 4)
    assert m["cost_usd"] == 1.5


def test_metrics_empty():
    m = metrics([])
    assert m["oldest_pending_ts"] is None
    assert m["scored"] == 0 and m["hit_rate"] is None
    assert m["baselines"]["majority"]["hit_rate"] is None
    assert m["direction_scores"]["brier"] is None and m["adverse_move_scores"]["brier"] is None


def test_local_source_summary_and_symbol_filter():
    f = ForwardLog(ForwardLogConfig(source="local", local_dir=str(FIXTURE)))
    s = f.summary()
    assert s["error"] is None and s["files"] == 3 and s["source"] == "local"
    assert s["overall"]["scored"] == 4
    assert s["overall"]["cost_usd"] == pytest.approx(6 * 8e-05)
    assert set(s["per_symbol"]) == {"BTC/USD", "ETH/USD"}
    btc, eth = s["per_symbol"]["BTC/USD"], s["per_symbol"]["ETH/USD"]
    assert (btc["decisions"], btc["scored"], btc["pending"], btc["abstain"]) == (4, 2, 1, 1)
    assert (eth["decisions"], eth["scored"], eth["error"]) == (3, 2, 1)
    eth_rows = f.rows(symbol="ETH/USD")
    assert len(eth_rows) == 3 and all(r["symbol"] == "ETH/USD" for r in eth_rows)
    assert len(f.rows(limit=2)) == 2


def test_max_days_keeps_newest_files():
    f = ForwardLog(ForwardLogConfig(source="local", local_dir=str(FIXTURE), max_days=1))
    rows = f.rows()
    assert len(rows) == 1 and rows[0]["candle_open"].startswith("2026-10-05")


def test_missing_local_dir_is_empty_with_error(tmp_path):
    f = ForwardLog(ForwardLogConfig(source="local", local_dir=str(tmp_path / "nope")))
    s = f.summary()
    assert s["overall"]["decisions"] == 0 and "does not exist" in s["error"]


def test_off_never_reads():
    def boom(*a):
        raise AssertionError("no fetch when off")
    s = ForwardLog(ForwardLogConfig(source="off"), http_get=boom).summary()
    assert s["source"] == "off" and s["overall"]["decisions"] == 0 and s["error"] is None


class FakeGitHub:
    """Serves the fixture directory the way the contents API and raw host would."""

    def __init__(self, root=FIXTURE, repo="me/repo", branch="data-log"):
        self.root, self.repo, self.branch = root, repo, branch
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    def __call__(self, url, headers, timeout_s):
        self.calls.append((url, headers))
        if self.fail:
            raise OSError("network down")
        api = f"https://api.github.com/repos/{self.repo}/contents/"
        raw = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/"
        if url.startswith(api) and "/" not in url[len(api):].split("?")[0]:
            folder = url[len(api):].split("?")[0]
            d = self.root / folder
            if not d.is_dir():
                raise NotFound(url)
            return json.dumps([{"name": p.name, "type": "file", "sha": str(p.stat().st_mtime_ns),
                                "download_url": f"{raw}{folder}/{p.name}?token=SECRET"}
                               for p in sorted(d.glob("*.jsonl"))]).encode()
        path = url[len(api):].split("?")[0] if url.startswith(api) else url[len(raw):] if url.startswith(raw) else None
        if path is None or not (self.root / path).is_file():
            raise NotFound(url)
        return (self.root / path).read_bytes()


def gh_cfg(**kw):
    return ForwardLogConfig(source="github", repo="me/repo", branch="data-log", **kw)


def test_github_source_public(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    gh = FakeGitHub()
    s = ForwardLog(gh_cfg(), http_get=gh).summary()
    assert s["error"] is None and s["overall"]["scored"] == 4 and s["location"] == "me/repo@data-log"
    files = [u for u, _ in gh.calls if u.endswith(".jsonl")]
    assert len(files) == 3 and all(u.startswith("https://raw.githubusercontent.com/") for u in files)
    assert all("Authorization" not in h for _, h in gh.calls)


def test_github_source_token_uses_contents_api(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    gh = FakeGitHub()
    s = ForwardLog(gh_cfg(), http_get=gh).summary()
    assert s["overall"]["scored"] == 4
    assert all(h["Authorization"] == "Bearer tok" for _, h in gh.calls)
    assert all(u.startswith("https://api.github.com/") for u, _ in gh.calls)


def test_github_cache_refresh_and_failure(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    now = [1000.0]
    gh = FakeGitHub()
    f = ForwardLog(gh_cfg(refresh_seconds=300), http_get=gh, clock=lambda: now[0])
    f.summary()
    n = len(gh.calls)
    now[0] += 100
    f.summary()
    assert len(gh.calls) == n            # cached
    now[0] += 300
    f.summary()
    assert len(gh.calls) == n + 2        # re-listed; unchanged files not downloaded again
    gh.fail = True
    now[0] += 300
    s = f.summary()
    assert s["overall"]["scored"] == 4 and "network down" in s["error"]   # last good data kept
    gh.fail = False
    now[0] += 60                         # failures retry sooner
    assert f.summary()["error"] is None


def test_github_no_outcomes_folder_yet(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "2026-10-05.jsonl").write_bytes(
        (FIXTURE / "decisions" / "2026-10-05.jsonl").read_bytes())
    s = ForwardLog(gh_cfg(), http_get=FakeGitHub(tmp_path)).summary()
    assert s["error"] is None and s["overall"]["pending"] == 1 and s["overall"]["scored"] == 0


class HiddenRepo(FakeGitHub):
    """GitHub's view of a private repo the caller can't see: 404 everywhere."""

    def __call__(self, url, headers, timeout_s):
        self.calls.append((url, headers))
        raise NotFound(url)


def test_github_404_explains_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    s = ForwardLog(gh_cfg(), http_get=HiddenRepo()).summary()
    assert s["overall"]["decisions"] == 0 and "set GITHUB_TOKEN" in s["error"]
    monkeypatch.setenv("GITHUB_TOKEN", "tok-without-access")
    s = ForwardLog(gh_cfg(), http_get=HiddenRepo()).summary()
    assert "can't see me/repo with GITHUB_TOKEN" in s["error"] and "Contents" in s["error"]


def test_github_404_explains_missing_branch_or_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    class Repo(FakeGitHub):
        def __init__(self, branch_exists):
            super().__init__(tmp_path)
            self.branch_exists = branch_exists

        def __call__(self, url, headers, timeout_s):
            if url == "https://api.github.com/repos/me/repo":
                return b"{}"
            if "/branches/" in url:
                if self.branch_exists:
                    return b"{}"
                raise NotFound(url)
            return super().__call__(url, headers, timeout_s)

    assert "no branch 'data-log'" in ForwardLog(gh_cfg(), http_get=Repo(False)).summary()["error"]
    assert "no decisions/ on me/repo@data-log" in ForwardLog(gh_cfg(), http_get=Repo(True)).summary()["error"]


def test_github_redacts_tokens_in_errors(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def leaky(url, headers, timeout_s):
        raise OSError(f"failed {url}?token=SECRET")
    s = ForwardLog(gh_cfg(), http_get=leaky).summary()
    assert s["error"] and "SECRET" not in s["error"]
