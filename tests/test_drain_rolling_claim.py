"""Drain priority is re-evaluated at every mine boundary, not once per pass (#293).

MEASURED ON THE DEPLOYED DAEMON, 2026-09-18 (oracle-issue-audit, read-only):

    .processing claimed   05:35:52 — 20 entries (projects 1 · convos 11 · session 8)
    7 docs/specs mines POSTed  07:12–07:17  → 1h37m after the claim
    docs/specs entries in flight                0
    live queue                          178 → 181 in ~37 min (growing)
    one transcript mine                 ≥ 427 s, still running
    one pass (20 serial)                ≥ 2.2 h  (a floor, not a mean)

#261 sorts projects-mode first, but only when a batch is claimed, and a pass is
serial and non-preemptible — so a priority-1 arrival waits a FULL PASS behind
re-mines it outranks.

THE INVARIANT, and it is about ORDER, not timing: an entry that arrives mid-pass
and outranks what is left runs NEXT — at most one mine of latency, never one
pass. Every assertion below reads the sequence of targets handed to the mine
runner, so nothing here depends on wall clock.

ABSORB BY REPLACEMENT: a boundary may swap a not-yet-run entry for a better
arrival, never grow the pass. If it could add, the cap would stop bounding
anything and "at most one pass" would become unbounded — which is why the cap
has its own test rather than being assumed.
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main


def _entry(dirname, mode="convos", deferrals=0):
    e = {"payload": {"dir": dirname, "wing": "w", "mode": mode}}
    if deferrals:
        e["drain_deferrals"] = deferrals
    return json.dumps(e)


class _DrainHarness(unittest.IsolatedAsyncioTestCase):
    """Drives the real `_drain_pending_mines` with the subprocess faked.

    The runner records every directory it is asked to mine, in order — that
    sequence IS the assertion surface. `inject` lets a test write new entries to
    the live queue *between* mines, which is the situation #293 is about.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.queue = os.path.join(self.tmp, "pending-mines.jsonl")
        self.ran: list = []
        self.inject_after: dict = {}
        self._patches = [
            patch.object(main, "_pending_mines_path", lambda: self.queue),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_load_mined_state", lambda: {}),
            patch.object(main, "_save_mined_state", lambda *_a, **_k: None),
            # The synthetic dirs are not real files. These two gates are about
            # path shape and re-mine dedup, neither of which this suite is
            # testing — the assertion surface is the ORDER of mines.
            patch.object(main, "_mineable_path_problem", lambda *_a, **_k: None),
            patch.object(main, "_drain_should_skip_unchanged", lambda *_a, **_k: False),
        ]
        for p in self._patches:
            p.start()
        self._mine_dirs = []

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self):
        async def _f(*args, **_kw):
            # The mine argv carries the interpreter, flags, the wing, and the
            # target dir. Pick the absolute path that does NOT exist on disk:
            # the interpreter does, our synthetic targets do not. Matching on a
            # "/d" prefix picked up the wing on one call and silently shifted
            # every injection key.
            cands = [a for a in args
                     if isinstance(a, str) and a.startswith("/") and not os.path.exists(a)]
            target = cands[0] if cands else "?"
            self.ran.append(target)
            for d in self.inject_after.pop(target, []):
                with open(self.queue, "a", encoding="utf-8") as f:
                    f.write(d + "\n")
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"mined 1 drawer", b""))
            proc.returncode = 0
            return proc
        return _f

    def write_queue(self, lines):
        with open(self.queue, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    async def drain(self):
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch(
            "asyncio.create_subprocess_exec", side_effect=self._spawn()
        ), patch.object(main, "_mine_sem", asyncio.Semaphore(1)):
            return await main._drain_pending_mines()


class TestPriorityIsReEvaluatedAtEveryBoundary(_DrainHarness):
    async def test_a_projects_entry_queued_mid_pass_runs_NEXT(self):
        """Invariant 1. Fails on the base — there it runs last, or not at all."""
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "6"}):
            self.write_queue([_entry(f"/d{i}") for i in range(6)])
            # a high-priority arrival lands while the 2nd mine is running
            self.inject_after["/d1"] = [_entry("/urgent", mode="projects")]
            await self.drain()
        self.assertIn("/urgent", self.ran, "the arrival never ran at all")
        pos = self.ran.index("/urgent")
        self.assertEqual(pos, 2,
                         f"arrival must run immediately after the mine it arrived during; "
                         f"ran order was {self.ran}")

    async def test_it_waits_at_most_ONE_mine_wherever_it_lands(self):
        for after in ("/d0", "/d2", "/d3"):
            with self.subTest(after=after):
                self.ran = []
                self.inject_after = {}
                with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "6"}):
                    self.write_queue([_entry(f"/d{i}") for i in range(6)])
                    self.inject_after[after] = [_entry("/urgent", mode="projects")]
                    await self.drain()
                gap = self.ran.index("/urgent") - self.ran.index(after)
                self.assertEqual(gap, 1, f"waited {gap} mines, order {self.ran}")


class TestTheCapStillBounds(_DrainHarness):
    async def test_a_pass_runs_at_most_cap_mines_even_under_constant_arrivals(self):
        """Absorb by REPLACEMENT. If a boundary could add, this is unbounded."""
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "5"}):
            self.write_queue([_entry(f"/d{i}") for i in range(5)])
            for i in range(5):
                self.inject_after[f"/d{i}"] = [_entry(f"/new{i}", mode="projects")]
            await self.drain()
        self.assertLessEqual(len(self.ran), 5, f"pass ran {len(self.ran)} mines: {self.ran}")

    async def test_nothing_is_dropped_set_equality_not_totals(self):
        """Invariant 3 — every entry either ran or is still queued, never neither."""
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "3"}):
            queued = [f"/d{i}" for i in range(6)]
            self.write_queue([_entry(d) for d in queued])
            self.inject_after["/d0"] = [_entry("/late", mode="projects")]
            await self.drain()
        left = set()
        if os.path.exists(self.queue):
            with open(self.queue, encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        left.add(json.loads(ln)["payload"]["dir"])
        self.assertEqual(set(queued) | {"/late"}, set(self.ran) | left,
                         f"ran={self.ran} left={sorted(left)}")
        self.assertFalse(set(self.ran) & left, "an entry both ran and stayed queued")


class TestDeferralsAndRecovery(_DrainHarness):
    async def test_displaced_entries_come_back_with_the_counter_bumped(self):
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "2"}):
            self.write_queue([_entry("/a"), _entry("/b"), _entry("/c")])
            self.inject_after["/a"] = [_entry("/urgent", mode="projects")]
            await self.drain()
        with open(self.queue, encoding="utf-8") as f:
            carried = [json.loads(ln) for ln in f if ln.strip()]
        self.assertTrue(carried, "displaced entries must return to the queue")
        self.assertTrue(all(int(e.get("drain_deferrals", 0)) >= 1 for e in carried),
                        f"deferral counter not bumped: {carried}")

    async def test_an_escaped_entry_outranks_a_projects_arrival(self):
        """#261's escape hatch still wins — priority must not starve anything."""
        esc = main._DRAIN_PRIORITY_ESCAPE_DEFERRALS
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "4"}):
            self.write_queue([_entry("/d0"), _entry("/starved", deferrals=esc), _entry("/d1")])
            self.inject_after["/starved"] = [_entry("/urgent", mode="projects")]
            await self.drain()
        self.assertEqual(self.ran[0], "/starved",
                         f"the escaped entry must lead the pass, order {self.ran}")

    async def test_processing_describes_exactly_what_is_still_owed(self):
        """Invariant 5 — #244 folds back the remainder, never a completed mine."""
        seen: list = []
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "4"}):
            self.write_queue([_entry(f"/d{i}") for i in range(4)])
            orig = main._rebalance_claim

            def _spy(owned, budget, path, proc_path):
                out = orig(owned, budget, path, proc_path)
                if os.path.exists(proc_path):
                    with open(proc_path, encoding="utf-8") as f:
                        seen.append([json.loads(l)["payload"]["dir"] for l in f if l.strip()])
                return out

            with patch.object(main, "_rebalance_claim", _spy):
                await self.drain()
        for ran_so_far, owed in zip(range(1, len(seen) + 1), seen):
            already = set(self.ran[:ran_so_far])
            self.assertFalse(already & set(owed),
                             f"a completed mine was still listed as owed: {owed}")


class TestBoundaryCostIsBounded(_DrainHarness):
    async def test_merge_cost_with_a_500_line_pending_file(self):
        """Invariant 8 — state the number with its unit; it must be negligible
        against a mine (measured floor: one transcript mine ≥ 427 s)."""
        owned = [(_entry(f"/own{i}") + "\n", json.loads(_entry(f"/own{i}"))) for i in range(20)]
        with open(self.queue, "w", encoding="utf-8") as f:
            for i in range(500):
                f.write(_entry(f"/arr{i}") + "\n")
        proc = self.queue + ".processing"
        t0 = time.monotonic()
        main._rebalance_claim(owned, 20, self.queue, proc)
        ms = (time.monotonic() - t0) * 1000.0
        print(f"\n    boundary merge, 500-line pending + 20 owned: {ms:.1f} ms")
        self.assertLess(ms, 250.0, f"boundary merge took {ms:.1f} ms")


class TestStatusMetrics(_DrainHarness):
    async def test_mine_status_reports_the_mechanism(self):
        """Invariant 9 — visible without reading code."""
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "4"}):
            self.write_queue([_entry(f"/d{i}") for i in range(3)])
            self.inject_after["/d0"] = [_entry("/urgent", mode="projects")]
            await self.drain()
        self.assertIsNotNone(main._DRAIN_BOUNDARY_STATE["last_boundary_merge_at"])
        self.assertGreaterEqual(main._DRAIN_BOUNDARY_STATE["arrivals_absorbed_last_pass"], 1)


if __name__ == "__main__":
    unittest.main()
