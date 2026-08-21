from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from agentsdock_team_hub.database import LATEST_SCHEMA_VERSION, open_database


def _multiprocess_open(path: str, output) -> None:
    try:
        connection = open_database(path)
        try:
            output.put(
                (
                    "ok",
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    len(connection.execute("PRAGMA foreign_key_check").fetchall()),
                )
            )
        finally:
            connection.close()
    except BaseException as exc:
        output.put(("error", type(exc).__name__, str(exc)))


class DatabaseConcurrencyTests(unittest.TestCase):
    def test_repeated_eight_way_first_open_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for round_number in range(50):
                path = root / f"threaded-{round_number}.sqlite3"

                def open_and_check(_: int) -> tuple[int, str, int]:
                    connection = open_database(path)
                    try:
                        return (
                            connection.execute("PRAGMA user_version").fetchone()[0],
                            connection.execute("PRAGMA integrity_check").fetchone()[0],
                            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
                        )
                    finally:
                        connection.close()

                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(open_and_check, range(8)))
                self.assertEqual(results, [(LATEST_SCHEMA_VERSION, "ok", 0)] * 8)
                reopened = open_database(path)
                try:
                    self.assertEqual(
                        reopened.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],
                        LATEST_SCHEMA_VERSION,
                    )
                finally:
                    reopened.close()

    def test_eight_processes_can_first_open_one_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "multiprocess.sqlite3")
            context = multiprocessing.get_context("spawn")
            output = context.Queue()
            processes = [
                context.Process(target=_multiprocess_open, args=(path, output))
                for _ in range(8)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive(), "database opener did not terminate")
                self.assertEqual(process.exitcode, 0)
            results = [output.get(timeout=5) for _ in processes]
            self.assertEqual(
                results,
                [("ok", LATEST_SCHEMA_VERSION, "ok", 0)] * len(processes),
            )


if __name__ == "__main__":
    unittest.main()
