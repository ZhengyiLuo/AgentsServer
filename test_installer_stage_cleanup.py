from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / "install.sh"


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


class InstallerStageCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        cls.cleanup_function = _between(
            source,
            "\ncleanup() {",
            "\ntrap cleanup EXIT",
        )

    def _run_cleanup(
        self,
        *,
        stage: Path,
        stage_device: int,
        stage_inode: int,
        transaction_id: str = "",
        rollback_settles: bool = False,
        failed: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        script = f"""
set -u
STAGE_DIR={shlex.quote(str(stage))}
STAGE_DIR_DEVICE={stage_device}
STAGE_DIR_INODE={stage_inode}
UV_INSTALLER=
IN_EXIT_CLEANUP=false
ACTIVATION_TRANSACTION_ID={shlex.quote(transaction_id)}
ACTIVATION_TRANSACTION_DIR={shlex.quote(str(stage.parent / '.activation-transaction'))}
ACTIVATION_TRANSACTION_PHASE=prepared
TEAM_HUB_RECOVERY_ATTEMPTED=false
TEAM_HUB_OPERATION_PENDING=false
TEAM_HUB_OPERATION_FINALIZED=false
TEAM_HUB_REACTIVATION_FENCE_PENDING=false
TEAM_HUB_REACTIVATION_FINALIZED=false
TEAM_HUB_COLD_GUARD_PENDING=false
SERVICE_STOPPED_FOR_COLD_HANDOFF=false
CANDIDATE_SERVICE_MAY_HAVE_STARTED=false
mask_install_signals() {{ :; }}
stop_active_stage() {{ :; }}
release_install_lock() {{ :; }}
team_hub_transaction_requires_recovery() {{ return 1; }}
restore_previous_release_transaction() {{
  {str(rollback_settles).lower()} || return 1
  rmdir "$ACTIVATION_TRANSACTION_DIR" || return 1
  ACTIVATION_TRANSACTION_ID=
}}
{self.cleanup_function}
{str(not failed).lower()}
cleanup
"""
        return subprocess.run(
            ["/bin/bash", "-c", script],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cleanup_removes_the_exact_owned_stage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            (stage / "candidate-marker").write_text("candidate\n", encoding="ascii")
            identity = stage.stat(follow_symlinks=False)

            result = self._run_cleanup(
                stage=stage,
                stage_device=identity.st_dev,
                stage_inode=identity.st_ino,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stage.exists())

    def test_unarmed_only_rejection_never_falls_through_to_exit_rollback(self) -> None:
        source = INSTALLER.read_text()
        block = _between(
            source,
            '\nif [[ "$ACTIVATION_TRANSACTION_RESUMED" == "true" ]]; then\n',
            '\nif [[ "$TEAM_HUB_OPERATION_PENDING" == "true" ]]; then',
        )
        for failure in ("load", "proof", "owner"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / "stage"
                stage.mkdir()
                journal = root / ".activation-transaction"
                journal.mkdir()
                trace = root / "native-action"
                script = f"""
set -eu
STAGE_DIR={shlex.quote(str(stage))}
STAGE_DIR_DEVICE={stage.stat().st_dev}
STAGE_DIR_INODE={stage.stat().st_ino}
ACTIVATION_TRANSACTION_DIR={shlex.quote(str(journal))}
ACTIVATION_TRANSACTION_ID=activation-aaaaaaaaaaaaaaaaaaaaaaaa
ACTIVATION_TRANSACTION_PHASE=prepared
ACTIVATION_TRANSACTION_RESUMED=true
RECOVER_UNARMED_ONLY=true
EXECUTION_MODE=split
CANDIDATE_RUNTIME_ROOT="$STAGE_DIR"
TEAM_HUB_RECOVERY_ATTEMPTED=false
TEAM_HUB_OPERATION_PENDING=false
TEAM_HUB_OPERATION_FINALIZED=false
TEAM_HUB_REACTIVATION_FENCE_PENDING=false
TEAM_HUB_REACTIVATION_FINALIZED=false
TEAM_HUB_COLD_GUARD_PENDING=false
SERVICE_STOPPED_FOR_COLD_HANDOFF=false
CANDIDATE_SERVICE_MAY_HAVE_STARTED=false
IN_EXIT_CLEANUP=false
UV_INSTALLER=
mask_install_signals() {{ :; }}
stop_active_stage() {{ :; }}
release_install_lock() {{ :; }}
team_hub_transaction_requires_recovery() {{ return 1; }}
load_pending_activation_transaction() {{ {'return 1' if failure == 'load' else ':'}; }}
execution_activation_command() {{ {'return 1' if failure == 'proof' else 'echo required'}; }}
native_action() {{ echo unsafe >> {shlex.quote(str(trace))}; }}
execution_recovery_arm() {{ native_action; }}
execution_recovery_command() {{ native_action; }}
recover_pending_activation_transaction() {{ native_action; }}
restore_previous_release_transaction() {{ native_action; }}
{self.cleanup_function}
trap cleanup EXIT
{block}
"""
                result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(trace.exists(), result.stderr)
                self.assertTrue(stage.exists())
                self.assertTrue(journal.exists())

    def test_failed_cleanup_preserves_stage_until_its_transaction_settles(self) -> None:
        for acknowledged, settles in ((True, False), (False, False), (True, True)):
            with self.subTest(acknowledged=acknowledged, settles=settles), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / "stage"
                stage.mkdir()
                marker = stage / "candidate-marker"
                marker.write_bytes(b"exact retained runtime\n")
                identity = stage.stat(follow_symlinks=False)
                journal = root / ".activation-transaction"
                journal.mkdir(mode=0o700)
                result = self._run_cleanup(
                    stage=stage, stage_device=identity.st_dev, stage_inode=identity.st_ino,
                    transaction_id="activation-" + "a" * 24 if acknowledged else "",
                    rollback_settles=settles, failed=True,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(journal.exists(), not settles)
                self.assertEqual(stage.exists(), not settles)
                if not settles:
                    self.assertEqual(marker.read_bytes(), b"exact retained runtime\n")
                    self.assertEqual(stage.stat().st_ino, identity.st_ino)

    def test_cleanup_never_removes_a_replacement_at_the_old_stage_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            release = root / "release"
            stage.mkdir()
            candidate = stage / "candidate-marker"
            candidate.write_text("candidate\n", encoding="ascii")
            identity = stage.stat(follow_symlinks=False)

            # Activation consumes the exact staged directory by renaming it to
            # the release path. A different owner can then claim the now-free,
            # predictable staging pathname before EXIT cleanup runs.
            stage.rename(release)
            stage.mkdir()
            sentinel = stage / "replacement-sentinel"
            sentinel.write_text("do not delete\n", encoding="ascii")

            result = self._run_cleanup(
                stage=stage,
                stage_device=identity.st_dev,
                stage_inode=identity.st_ino,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                sentinel.exists(),
                "EXIT cleanup removed a replacement that did not match the captured "
                "staging-directory identity",
            )
            self.assertEqual(sentinel.read_text(encoding="ascii"), "do not delete\n")
            self.assertEqual(
                (
                    release.stat(follow_symlinks=False).st_dev,
                    release.stat(follow_symlinks=False).st_ino,
                ),
                (identity.st_dev, identity.st_ino),
            )
            self.assertEqual(
                (release / candidate.name).read_text(encoding="ascii"),
                "candidate\n",
            )


if __name__ == "__main__":
    unittest.main()
