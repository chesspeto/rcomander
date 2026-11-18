import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.widgets import DataTable

from app import Commander, Confirm, CopyMethod, Entry, FilePanel, Location, PanelType, Prompt, human_size, list_location, progress_command


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_human_sizes_and_progress_stream(self):
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1024 ** 2), "1.0 MiB")
        self.assertEqual(human_size(1536 * 1024 ** 2), "1.5 GiB")
        events = []
        code, output = await progress_command(
            "/usr/bin/python3", "-c", "import sys; sys.stdout.write(' 10%\\r 95%\\rfallo\\n')",
            on_progress=lambda percent, text: events.append(percent),
        )
        self.assertEqual(code, 0)
        self.assertEqual(events, [10, 95])
        self.assertIn("fallo", output)

    async def test_real_rsync_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "archivo"
            target = root / "destino"
            source.write_bytes(b"x" * 65536)
            target.mkdir()
            values = []
            code, output = await progress_command(
                "rsync", "-a", "--info=progress2", "--", str(source), str(target) + "/",
                on_progress=lambda percent, text: values.append(percent),
            )
            self.assertEqual(code, 0, output)
            self.assertIn(100, values)
            self.assertEqual((target / source.name).read_bytes(), source.read_bytes())

    async def test_mark_multiple_copy_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "src", Path(tmp) / "dst"
            source.mkdir()
            target.mkdir()
            for name in ("a", "b", "c"):
                (source / name).write_text(name)
            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left, right = app.query_one("#left", FilePanel), app.query_one("#right", FilePanel)
                await left.refresh_location(Location(str(source)))
                await right.refresh_location(Location(str(target)))
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                await pilot.press("insert", "insert")
                self.assertEqual(left.marked, {"a", "b"})
                await pilot.press("f5")
                await pilot.pause()
                await pilot.click("#rsync")
                await pilot.pause()
                await pilot.click("#yes")
                await pilot.pause(1)
                self.assertEqual(sorted(p.name for p in target.iterdir()), ["a", "b"])
                self.assertEqual(left.marked, set())
                left.query_one(DataTable).move_cursor(row=1)
                await pilot.press("insert", "insert", "f8")
                await pilot.pause()
                await pilot.click("#yes")
                await pilot.pause()
                self.assertEqual(sorted(p.name for p in source.iterdir()), ["c"])
                right.query_one(DataTable).focus()
                right.query_one(DataTable).move_cursor(row=1)
                await pilot.press("insert", "insert", "f6")
                await pilot.pause()
                await pilot.click("#yes")
                await pilot.pause()
                self.assertEqual(sorted(p.name for p in source.iterdir()), ["a", "b", "c"])
                self.assertEqual(list(target.iterdir()), [])

    async def test_cancel_transfer_stops_process_and_keeps_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "src", Path(tmp) / "dst"
            source.mkdir()
            target.mkdir()
            (source / "a").write_text("a")
            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left, right = app.query_one("#left", FilePanel), app.query_one("#right", FilePanel)
                await left.refresh_location(Location(str(source)))
                await right.refresh_location(Location(str(target)))
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                await pilot.press("insert")
                async def slow_copy(*args, **kwargs):
                    return await progress_command("/usr/bin/python3", "-c", "import time; time.sleep(30)", **kwargs)
                with patch("app.progress_command", side_effect=slow_copy):
                    await pilot.press("f5")
                    await pilot.pause()
                    await pilot.click("#rsync")
                    await pilot.pause()
                    await pilot.click("#yes")
                    await pilot.pause()
                    self.assertIsNotNone(app.transfer_process)
                    await pilot.press("ctrl+x")
                    await pilot.pause(1)
                    self.assertFalse(app.transfer_active)
                    self.assertEqual(left.marked, {"a"})

    async def test_listing_and_rsync_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst = root / "origen", root / "destino"
            src.mkdir()
            dst.mkdir()
            (src / "archivo con espacios.txt").write_text("contenido")
            location, entries = await list_location(Location(str(src)))
            self.assertEqual(location.path, str(src))
            self.assertEqual([e.name for e in entries], ["..", "archivo con espacios.txt"])

            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left = app.query_one("#left", FilePanel)
                right = app.query_one("#right", FilePanel)
                await left.refresh_location(Location(str(src)))
                await right.refresh_location(Location(str(dst)))
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                await pilot.press("f5")
                await pilot.pause()
                self.assertIsInstance(app.screen, CopyMethod)
                await pilot.click("#rsync")
                await pilot.pause()
                self.assertIsInstance(app.screen, Confirm)
                await pilot.click("#yes")
                await pilot.pause(1)
                self.assertEqual((dst / "archivo con espacios.txt").read_text(), "contenido")
                await pilot.press("f7")
                await pilot.pause()
                self.assertIsInstance(app.screen, Prompt)
                await pilot.press("n", "u", "e", "v", "o", "enter")
                await pilot.pause()
                self.assertTrue((src / "nuevo").is_dir())
                await pilot.press("f2")
                await pilot.pause()
                self.assertIsInstance(app.screen, PanelType)
                await pilot.click("#remote")
                await pilot.pause()
                self.assertIsInstance(app.screen, Prompt)
                await pilot.press("escape")

    async def test_panel_selection_and_delete_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            file = path / "archivo.txt"
            file.write_text("datos")
            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left = app.query_one("#left", FilePanel)
                right = app.query_one("#right", FilePanel)
                await left.refresh_location(Location(str(path)))
                await right.refresh_location(Location(str(path)))
                right.query_one(DataTable).focus()
                await pilot.press("f1")
                await pilot.pause()
                await pilot.click("#local")
                await pilot.pause()
                self.assertEqual(left.location.path, str(Path.home()))
                self.assertEqual(right.location.path, str(path))
                right.query_one(DataTable).focus()
                right.query_one(DataTable).move_cursor(row=1)
                await pilot.press("f8")
                await pilot.pause()
                await pilot.click("#no")
                await pilot.pause()
                self.assertTrue(file.exists())
                await pilot.press("f8")
                await pilot.pause()
                await pilot.click("#yes")
                await pilot.pause()
                self.assertFalse(file.exists())

    async def test_remote_listing_keeps_password(self):
        remote = Location(".", "user@example", " contraseña ")
        with patch("app.command", new_callable=AsyncMock, return_value=(0, '["/home/user", [["carpeta", true, 0]]]', "")) as run:
            location, entries = await list_location(remote)
        self.assertEqual(location.password, remote.password)
        self.assertEqual(location.path, "/home/user")
        self.assertTrue(entries[1].directory)
        self.assertEqual(run.call_args.kwargs["password"], remote.password)
        self.assertNotIn(remote.password, run.call_args.args)

    async def test_edit_opens_selected_file_in_vim(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "archivo con espacios.txt"
            file.write_text("datos")
            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left = app.query_one("#left", FilePanel)
                await left.refresh_location(Location(tmp))
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                with patch.object(app, "suspend", return_value=nullcontext()), patch("app.subprocess.run") as run:
                    run.return_value.returncode = 0
                    await pilot.press("f4")
                    await pilot.pause()
                    self.assertEqual(run.call_args.args[0], ["vim", "--", str(file)])

    async def test_recursive_deletion_and_arrow_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "carpeta"
            (folder / "subcarpeta").mkdir(parents=True)
            (folder / "subcarpeta" / "archivo").write_text("datos")
            app = Commander()
            async with app.run_test(size=(120, 35)) as pilot:
                left = app.query_one("#left", FilePanel)
                await left.refresh_location(Location(tmp))
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                await pilot.press("f8")
                await pilot.pause()
                self.assertEqual(app.focused.id, "no")
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(folder.exists())
                await pilot.press("f8")
                await pilot.pause()
                await pilot.press("left")
                self.assertEqual(app.focused.id, "yes")
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(folder.exists())
                await pilot.press("f1")
                await pilot.pause()
                await pilot.press("right")
                self.assertEqual(app.focused.id, "remote")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, Prompt)
                await pilot.press("escape")

    async def test_remote_recursive_delete_quotes_path(self):
        app = Commander()
        async with app.run_test(size=(120, 35)) as pilot:
            left = app.query_one("#left", FilePanel)
            location = Location("/home/user", "user@example", "clave")
            entries = [Entry("..", True), Entry("carpeta ' especial", True)]
            with patch("app.list_location", new_callable=AsyncMock, return_value=(location, entries)):
                await left.refresh_location(location)
                left.query_one(DataTable).focus()
                left.query_one(DataTable).move_cursor(row=1)
                with patch("app.command", new_callable=AsyncMock, return_value=(0, "", "")) as run:
                    await pilot.press("f8")
                    await pilot.pause()
                    await pilot.press("left", "enter")
                    await pilot.pause()
                    self.assertEqual(run.call_args.args[-1], "rm -r -- '/home/user/carpeta '\"'\"' especial'")
                    self.assertEqual(run.call_args.kwargs["password"], "clave")

    async def test_panel_remote_connection_starts_in_home(self):
        app = Commander()
        async with app.run_test(size=(120, 35)) as pilot:
            right = app.query_one("#right", FilePanel)
            async def remote_listing(location):
                self.assertEqual(location.path, ".")
                self.assertEqual(location.host, "user@server")
                self.assertEqual(location.password, "secreto")
                return Location("/home/user", location.host, location.password), [Entry("..", True)]
            with patch("app.list_location", side_effect=remote_listing):
                await pilot.press("f2")
                await pilot.pause()
                await pilot.click("#remote")
                await pilot.pause()
                app.screen.query_one("#answer").value = "user@server"
                await pilot.press("enter")
                await pilot.pause()
                app.screen.query_one("#answer").value = "secreto"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(right.location.path, "/home/user")
                self.assertEqual(right.location.host, "user@server")

    async def test_console_uses_remote_panel_and_refreshes(self):
        app = Commander()
        async with app.run_test(size=(120, 35)) as pilot:
            left = app.query_one("#left", FilePanel)
            location = Location("/home/user/espacio ' uno", "user@server", "secreto")
            with patch("app.list_location", new_callable=AsyncMock, return_value=(location, [Entry("..", True)])) as listing:
                await left.refresh_location(location)
                left.query_one(DataTable).focus()
                with patch.object(app, "suspend", return_value=nullcontext()), patch("app.subprocess.run") as run:
                    run.return_value.returncode = 0
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    args = run.call_args.args[0]
                    self.assertIn("user@server", args)
                    self.assertEqual(args[-1], "cd -- '/home/user/espacio '\"'\"' uno' && exec \"${SHELL:-/bin/sh}\" -i")
                    self.assertEqual(run.call_args.kwargs["env"]["SSHPASS"], "secreto")
                    self.assertGreaterEqual(listing.await_count, 2)


if __name__ == "__main__":
    unittest.main()
