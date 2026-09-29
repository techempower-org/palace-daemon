"""The hook files a transcript under its LAUNCH directory, not its hook-time cwd.

Before: ``_project_wing`` preferred ``data["cwd"]``, which is wherever the
session is ``cd``'d when a Stop/PreCompact hook fires. On the palace host the
fleet-lead transcript (launched in $HOME) was queued as candela, projects,
memorypalace and goals_2026_09_28_overnight. 61 transcripts were split across
2+ wings, and each wing change re-filed the whole file.

Run with::

    python -m pytest tests/test_hook_launch_wing.py -q
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


class LaunchIdentityWing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "jp"
        for d in ("tapstone", "candela", "memorypalace", "familiar.realm.watch", "palace", "palace-daemon"):
            (self.home / "Projects" / d).mkdir(parents=True)
        (self.home / "dotfiles").mkdir()
        self._patch = patch.object(Path, "home", return_value=self.home)
        self._patch.start()
        self.enc = hook._encode_like_claude if hasattr(hook, "_encode_like_claude") else (
            lambda p: __import__("re").sub(r"[^A-Za-z0-9]", "-", p))

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _transcript(self, launch_dir: Path, *tail: str) -> str:
        return str(self.home / ".claude" / "projects" / self.enc(str(launch_dir)) / Path(*tail or ("s.jsonl",)))

    def test_launch_dir_beats_hook_time_cwd(self):
        """A session launched in tapstone that cd'd into candela stays tapstone."""
        t = self._transcript(self.home / "Projects" / "tapstone")
        data = {"cwd": str(self.home / "Projects" / "candela")}
        self.assertEqual(hook._project_wing(data, t), "tapstone")

    def test_home_launch_is_the_home_wing_wherever_it_wanders(self):
        t = self._transcript(self.home)
        for cwd in ("Projects/candela", "Projects/memorypalace", ".claude/projects/x/scratch/goals"):
            with self.subTest(cwd=cwd):
                self.assertEqual(hook._project_wing({"cwd": str(self.home / cwd)}, t), "jp")

    def test_subdirectory_launch_maps_to_its_project(self):
        t = self._transcript(self.home / "Projects" / "tapstone" / "scratch" / "ftarget-fw-kestrel")
        self.assertEqual(hook._project_wing({}, t), "tapstone")

    def test_worktree_launch_maps_to_its_project(self):
        t = self._transcript(self.home / "Projects" / "memorypalace" / ".claude" / "worktrees" / "agent-a1")
        self.assertEqual(hook._project_wing({}, t), "memorypalace")

    def test_dotted_project_name_round_trips(self):
        t = self._transcript(self.home / "Projects" / "familiar.realm.watch")
        self.assertEqual(hook._project_wing({}, t), "familiar_realm_watch")

    def test_longest_project_match_wins(self):
        t = self._transcript(self.home / "Projects" / "palace-daemon" / "clients")
        self.assertEqual(hook._project_wing({}, t), "palace_daemon")

    def test_subagent_transcript_nested_deeper_uses_the_same_folder(self):
        t = self._transcript(self.home / "Projects" / "tapstone", "sid-1", "subagents", "agent-7.jsonl")
        self.assertEqual(hook._project_wing({"cwd": str(self.home / "Projects" / "candela")}, t), "tapstone")

    def test_home_top_level_and_hidden_dirs(self):
        self.assertEqual(hook._project_wing({}, self._transcript(self.home / "dotfiles")), "dotfiles")
        self.assertEqual(hook._project_wing({}, self._transcript(self.home / ".claude" / "x")), "jp")

    def test_symlinked_project_maps_to_its_target(self):
        """~/Projects/storyvox -> candela: the palace wing was renamed to candela (#302).

        Matching the link name re-created the retired ``storyvox`` wing and split the project.
        """
        (self.home / "Projects" / "storyvox").symlink_to(self.home / "Projects" / "candela")
        for launch in ("storyvox", "storyvox/app/src"):
            with self.subTest(launch=launch):
                t = self._transcript(self.home / "Projects" / launch)
                self.assertEqual(hook._project_wing({}, t), "candela")

    def test_relative_and_nested_symlink_targets(self):
        projects = self.home / "Projects"
        (projects / "2g" / "microcell").mkdir(parents=True)
        (projects / "3g").symlink_to("2g")  # relative link, as on katana
        (projects / "microcell").symlink_to(projects / "2g" / "microcell")
        self.assertEqual(hook._project_wing({}, self._transcript(projects / "3g")), "2g")
        self.assertEqual(hook._project_wing({}, self._transcript(projects / "microcell")), "2g")

    def test_symlink_leaving_projects_keeps_the_link_name(self):
        outside = Path(self._tmp.name) / "elsewhere" / "thing"
        outside.mkdir(parents=True)
        (self.home / "Projects" / "thing-link").symlink_to(outside)
        t = self._transcript(self.home / "Projects" / "thing-link")
        self.assertEqual(hook._project_wing({}, t), "thing_link")

    def test_no_transcript_path_still_uses_cwd(self):
        """Unchanged fallback: without a transcript path the cwd rule applies."""
        data = {"cwd": str(self.home / "Projects" / "candela" / "src")}
        self.assertEqual(hook._project_wing(data, ""), "candela")

    def test_non_claude_transcript_path_falls_back_to_cwd(self):
        data = {"cwd": str(self.home / "Projects" / "candela")}
        self.assertEqual(hook._project_wing(data, "/var/log/something.jsonl"), "candela")


if __name__ == "__main__":
    unittest.main()
