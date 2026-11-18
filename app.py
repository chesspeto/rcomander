"""Gestor de archivos de dos paneles para Linux y SSH."""
from __future__ import annotations

import asyncio
import codecs
import errno
import json
import os
import posixpath
import pty
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, Static
from textual import work


@dataclass
class Entry:
    name: str
    directory: bool
    size: int = 0


@dataclass
class Location:
    path: str
    host: str = ""
    password: str = field(default="", repr=False)

    @property
    def title(self) -> str:
        return f"{self.host}:{self.path}" if self.host else self.path


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


REMOTE_LIST = r'''import json, os, sys
p = os.path.expanduser(sys.argv[1])
p = os.path.abspath(p)
entries = []
with os.scandir(p) as it:
    for e in it:
        try:
            s = e.stat(follow_symlinks=False)
            entries.append([e.name, e.is_dir(), s.st_size])
        except OSError:
            entries.append([e.name, False, 0])
print(json.dumps([p, entries]))
'''


async def command(*args: str, password: str = "") -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("SSHPASS", None)
    if password:
        env["SSHPASS"] = password
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def progress_command(*args: str, password: str = "", on_progress: Callable[[int, str], None], on_start: Callable | None = None) -> tuple[int, str]:
    """Read carriage-return progress from rsync and scp using a pseudo-terminal."""
    env = os.environ.copy()
    env.pop("SSHPASS", None)
    env["LC_ALL"] = "C"
    if password:
        env["SSHPASS"] = password
    master, slave = pty.openpty()
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, start_new_session=True
            )
        finally:
            os.close(slave)
        if on_start:
            on_start(process)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        output = ""
        while True:
            try:
                chunk = await asyncio.to_thread(os.read, master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            text = decoder.decode(chunk)
            output = (output + text)[-32768:]
            pending += text
            lines = re.split(r"[\r\n]", pending)
            pending = lines.pop()[-4096:]
            for line in lines:
                match = re.search(r"(?<!\d)(100|\d{1,2})%", line)
                if match:
                    on_progress(int(match.group(1)), line.strip())
        return await process.wait(), output
    finally:
        os.close(master)


def ssh_options(password: str) -> list[str]:
    return ["-o", "BatchMode=" + ("no" if password else "yes"),
            "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "NumberOfPasswordPrompts=1"]


def ssh_prefix(password: str) -> list[str]:
    if password and not shutil.which("sshpass"):
        raise RuntimeError("Instala sshpass: sudo apt install sshpass")
    return ["sshpass", "-e"] if password else []


async def list_location(location: Location) -> tuple[Location, list[Entry]]:
    if location.host:
        remote_command = "python3 -c " + shlex.quote(REMOTE_LIST) + " " + shlex.quote(location.path)
        code, out, err = await command(*ssh_prefix(location.password), "ssh", *ssh_options(location.password), "--", location.host, remote_command, password=location.password)
        if code:
            raise RuntimeError(err.strip() or "No se pudo listar el directorio remoto")
        path, rows = json.loads(out)
        entries = [Entry(*row) for row in rows]
        location = replace(location, path=path)
    else:
        path = os.path.abspath(os.path.expanduser(location.path))
        entries = []
        with os.scandir(path) as iterator:
            for item in iterator:
                try:
                    entries.append(Entry(item.name, item.is_dir(), item.stat(follow_symlinks=False).st_size))
                except OSError:
                    entries.append(Entry(item.name, False))
        location = Location(path)
    entries.sort(key=lambda entry: (not entry.directory, entry.name.casefold()))
    return location, [Entry("..", True), *entries]


class Prompt(ModalScreen[str | None]):
    def __init__(self, title: str, value: str = "", secret: bool = False) -> None:
        super().__init__()
        self.title_text, self.value = title, value
        self.secret = secret

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.value, id="answer", password=self.secret)
            yield Label("Enter: aceptar · Esc: cancelar")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value if self.secret else event.value.strip())

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


def navigate_buttons(screen: ModalScreen, event) -> None:
    if event.key in ("left", "up", "right", "down"):
        if event.key in ("left", "up"):
            screen.focus_previous()
        else:
            screen.focus_next()
        event.stop()
        event.prevent_default()


class Confirm(ModalScreen[bool]):
    def __init__(self, text: str, *, default_cancel: bool = False) -> None:
        super().__init__()
        self.text = text
        self.default_cancel = default_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.text)
            with Horizontal():
                yield Button("Aceptar", variant="primary", id="yes")
                yield Button("Cancelar", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def on_mount(self) -> None:
        if self.default_cancel:
            self.query_one("#no", Button).focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
        else:
            navigate_buttons(self, event)


class CopyMethod(ModalScreen[str | None]):
    def __init__(self, local: bool) -> None:
        super().__init__()
        self.local = local

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Elige el método de copia (Esc: cancelar)")
            with Horizontal():
                if self.local:
                    yield Button("Directa", id="direct")
                yield Button("Rsync", id="rsync")
                yield Button("SCP", id="scp")
                yield Button("Cancelar", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        else:
            navigate_buttons(self, event)


class PanelType(ModalScreen[str | None]):
    def __init__(self, side: str) -> None:
        super().__init__()
        self.side = side

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Panel {self.side}: elige ubicación (Esc: cancelar)")
            with Horizontal():
                yield Button("Local", id="local")
                yield Button("Remoto SSH", id="remote")
                yield Button("Cancelar", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        else:
            navigate_buttons(self, event)


class FilePanel(Vertical):
    def __init__(self, location: Location, **kwargs) -> None:
        super().__init__(**kwargs)
        self.location = location
        self.entries: list[Entry] = []
        self.loading = False
        self.marked: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static(self.location.title, classes="path")
        yield DataTable(cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Nombre", "Tamaño")

    async def refresh_location(self, location: Location | None = None) -> None:
        if self.loading:
            return
        self.loading = True
        try:
            new_location, entries = await list_location(location or self.location)
            if (new_location.host, new_location.path) != (self.location.host, self.location.path):
                self.marked.clear()
            self.marked.intersection_update(entry.name for entry in entries)
            self.location, self.entries = new_location, entries
            self.query_one(Static).update(self.location.title)
            table = self.query_one(DataTable)
            table.clear()
            for entry in entries:
                table.add_row(self.entry_label(entry), "<DIR>" if entry.directory else human_size(entry.size))
        except (OSError, ValueError, RuntimeError) as exc:
            self.app.notify(str(exc), severity="error", timeout=10)
        finally:
            self.loading = False

    def selected(self) -> Entry | None:
        index = self.query_one(DataTable).cursor_row
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def entry_label(self, entry: Entry) -> str:
        return ("* " if entry.name in self.marked else "  ") + ("📁 " if entry.directory else "   ") + entry.name

    def toggle_mark(self) -> None:
        table = self.query_one(DataTable)
        entry = self.selected()
        if not entry or entry.name == "..":
            return
        if entry.name in self.marked:
            self.marked.remove(entry.name)
        else:
            self.marked.add(entry.name)
        table.update_cell_at((table.cursor_row, 0), self.entry_label(entry))
        table.move_cursor(row=min(table.cursor_row + 1, len(self.entries) - 1))

    def selected_entries(self) -> list[Entry]:
        if self.marked:
            return [entry for entry in self.entries if entry.name in self.marked]
        entry = self.selected()
        return [entry] if entry and entry.name != ".." else []

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        entry = self.selected()
        if entry and entry.directory:
            path = posixpath.normpath(posixpath.join(self.location.path, entry.name))
            await self.refresh_location(replace(self.location, path=path))


class Commander(App):
    TITLE = "RCommander α"
    CSS = """
    Screen { background: #001a70; color: white; }
    #panels { height: 1fr; }
    FilePanel { width: 1fr; border: solid #65c8ff; }
    FilePanel:focus-within { border: double yellow; }
    .path { height: 1; background: #006b80; color: white; text-style: bold; }
    DataTable { height: 1fr; background: #001a70; }
    #status { height: 2; background: #006b80; padding: 0 1; }
    #progress { height: 1; display: none; }
    Prompt, Confirm, CopyMethod, PanelType { align: center middle; background: #000000 60%; }
    #dialog { width: 72; height: auto; padding: 1 2; border: double cyan; background: #003080; }
    #dialog Horizontal { height: 3; }
    #dialog Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("tab", "switch_panel", "Panel", priority=True),
        Binding("f1", "configure_left", "Izquierdo", priority=True),
        Binding("f2", "configure_right", "Derecho", priority=True),
        Binding("f3", "goto", "Ruta", priority=True),
        Binding("f4", "edit", "Editar", priority=True),
        Binding("f5", "copy", "Copiar", priority=True),
        Binding("f6", "move", "Mover", priority=True),
        Binding("f7", "mkdir", "Crear directorio", priority=True),
        Binding("f8", "delete", "Eliminar", priority=True),
        Binding("f9", "refresh_panels", "Actualizar", priority=True),
        Binding("f10", "quit", "Salir", priority=True),
        Binding("ctrl+s", "ssh_console", "Consola SSH", priority=True),
        Binding("insert", "mark", "Marcar", priority=True),
        Binding("ctrl+x", "cancel_transfer", "Cancelar copia", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panels"):
            yield FilePanel(Location(str(Path.home())), id="left")
            yield FilePanel(Location(str(Path.home())), id="right")
        yield Static("Enter: abrir carpeta · Tab: cambiar panel · F1/F2: configurar panel", id="status")
        yield ProgressBar(total=100, show_eta=False, id="progress")
        yield Footer()

    async def on_mount(self) -> None:
        self.transfer_active = False
        self.transfer_cancel_allowed = False
        self.transfer_cancelled = False
        self.transfer_process = None
        self.query_one("#left DataTable", DataTable).focus()
        await asyncio.gather(*(panel.refresh_location() for panel in self.query(FilePanel)))

    def panels(self) -> tuple[FilePanel, FilePanel]:
        left, right = self.query_one("#left", FilePanel), self.query_one("#right", FilePanel)
        return (right, left) if self.focused is right.query_one(DataTable) else (left, right)

    def action_switch_panel(self) -> None:
        if isinstance(self.screen, (Prompt, Confirm, CopyMethod, PanelType)):
            self.screen.focus_next()
        else:
            self.panels()[1].query_one(DataTable).focus()

    def action_mark(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.panels()[0].toggle_mark()

    def action_cancel_transfer(self) -> None:
        if not self.transfer_active or not self.transfer_cancel_allowed:
            return
        self.transfer_cancelled = True
        process = self.transfer_process
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.query_one("#status", Static).update("Cancelando transferencia…")

    @work
    async def action_configure_left(self) -> None:
        await self.configure_panel("left")

    @work
    async def action_configure_right(self) -> None:
        await self.configure_panel("right")

    async def configure_panel(self, side: str) -> None:
        panel = self.query_one(f"#{side}", FilePanel)
        choice = await self.push_screen_wait(PanelType("izquierdo" if side == "left" else "derecho"))
        if choice == "local":
            await panel.refresh_location(Location(str(Path.home())))
            panel.query_one(DataTable).focus()
            return
        if choice != "remote":
            return
        credentials = await self.prompt_ssh_credentials()
        if credentials is not None:
            host, password = credentials
            await panel.refresh_location(Location(".", host, password))
            panel.query_one(DataTable).focus()

    async def prompt_ssh_credentials(self) -> tuple[str, str] | None:
        host = await self.push_screen_wait(Prompt("SSH: usuario@servidor o alias de ~/.ssh/config"))
        if not host:
            return None
        if host.startswith("-") or any(c.isspace() for c in host):
            self.notify("Nombre de servidor inválido", severity="error")
            return None
        password = await self.push_screen_wait(Prompt("Contraseña SSH (vacía para usar clave o agente)", secret=True))
        if password is None:
            return None
        return host, password

    @work
    async def action_ssh_console(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        panel = self.panels()[0]
        location = panel.location
        if not location.host:
            credentials = await self.prompt_ssh_credentials()
            if credentials is None:
                return
            host, password = credentials
            location = Location(".", host, password)
            remote_command = None
        else:
            remote_command = "cd -- " + shlex.quote(location.path) + ' && exec "${SHELL:-/bin/sh}" -i'
        try:
            args = [*ssh_prefix(location.password), "ssh", *ssh_options(location.password), "-t", "--", location.host]
            if remote_command:
                args.append(remote_command)
            env = os.environ.copy()
            env.pop("SSHPASS", None)
            if location.password:
                env["SSHPASS"] = location.password
            with self.suspend():
                result = subprocess.run(args, env=env, check=False)
            if result.returncode:
                self.notify(f"SSH terminó con código {result.returncode}", severity="error")
            if panel.location.host:
                await panel.refresh_location()
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error", timeout=10)

    @work
    async def action_edit(self) -> None:
        panel = self.panels()[0]
        entry = panel.selected()
        if not entry or entry.directory or entry.name == "..":
            self.notify("Selecciona un archivo para editar", severity="warning")
            return
        path = posixpath.join(panel.location.path, entry.name)
        password = panel.location.password
        try:
            if panel.location.host:
                args = [*ssh_prefix(password), "ssh", *ssh_options(password), "-t", "--", panel.location.host, "vim -- " + shlex.quote(path)]
            else:
                args = ["vim", "--", path]
            env = os.environ.copy()
            env.pop("SSHPASS", None)
            if password:
                env["SSHPASS"] = password
            with self.suspend():
                result = subprocess.run(args, env=env, check=False)
            if result.returncode:
                self.notify(f"Vim o SSH terminó con código {result.returncode}", severity="error")
            await panel.refresh_location()
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error", timeout=10)

    @work
    async def action_delete(self) -> None:
        panel = self.panels()[0]
        entries = panel.selected_entries()
        if not entries:
            self.notify("Selecciona un archivo o directorio para eliminar", severity="warning")
            return
        names = "\n".join(entry.name for entry in entries[:10])
        if len(entries) > 10:
            names += f"\n… y {len(entries) - 10} más"
        if not await self.push_screen_wait(Confirm(f"¿Eliminar {len(entries)} elemento(s) de {panel.location.title}?\nLos directorios se eliminan con todo su contenido.\n{names}", default_cancel=True)):
            return
        try:
            for entry in entries:
                path = posixpath.join(panel.location.path, entry.name)
                if panel.location.host:
                    password = panel.location.password
                    remote_command = ("rm -r -- " if entry.directory else "rm -- ") + shlex.quote(path)
                    code, out, err = await command(*ssh_prefix(password), "ssh", *ssh_options(password), "--", panel.location.host, remote_command, password=password)
                    if code:
                        raise RuntimeError(err.strip() or out.strip() or "No se pudo eliminar el elemento")
                else:
                    if entry.directory and not os.path.islink(path):
                        await asyncio.to_thread(shutil.rmtree, path)
                    else:
                        await asyncio.to_thread(os.remove, path)
                panel.marked.discard(entry.name)
            self.notify(f"Eliminados: {len(entries)} elemento(s)")
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error", timeout=10)
        finally:
            await panel.refresh_location()

    @work
    async def action_goto(self) -> None:
        panel = self.panels()[0]
        path = await self.push_screen_wait(Prompt("Ir a directorio", panel.location.path))
        if path:
            await panel.refresh_location(replace(panel.location, path=path))

    async def action_refresh_panels(self) -> None:
        await asyncio.gather(*(panel.refresh_location() for panel in self.query(FilePanel)))

    @work
    async def action_copy(self) -> None:
        source, target = self.panels()
        method = await self.push_screen_wait(CopyMethod(not source.location.host and not target.location.host))
        if method:
            await self.transfer(method)

    @work
    async def action_mkdir(self) -> None:
        panel = self.panels()[0]
        name = await self.push_screen_wait(Prompt("Nombre del nuevo directorio"))
        if name is None or not name:
            return
        if name in (".", "..") or "/" in name or "\x00" in name:
            self.notify("Introduce un nombre de directorio, no una ruta", severity="error")
            return
        path = posixpath.join(panel.location.path, name)
        try:
            if panel.location.host:
                password = panel.location.password
                code, out, err = await command(*ssh_prefix(password), "ssh", *ssh_options(password), "--", panel.location.host, "mkdir -- " + shlex.quote(path), password=password)
                if code:
                    raise RuntimeError(err.strip() or out.strip() or "No se pudo crear el directorio")
            else:
                await asyncio.to_thread(os.mkdir, path)
            self.notify("Directorio creado")
            await panel.refresh_location()
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error", timeout=10)

    @work
    async def action_move(self) -> None:
        await self.transfer("move")

    async def transfer(self, method: str) -> None:
        source, target = self.panels()
        entries = source.selected_entries()
        if not entries:
            self.notify("Selecciona un archivo o directorio", severity="warning")
            return
        if source.loading or target.loading:
            self.notify("Espera a que termine la lectura de los paneles", severity="warning")
            return
        if source.location.host and target.location.host:
            self.notify("Las transferencias entre dos paneles remotos aún no están disponibles", severity="warning")
            return
        if method == "move" and (source.location.host or target.location.host):
            self.notify("Mover solo está disponible entre paneles locales", severity="warning")
            return
        if self.transfer_active:
            self.notify("Ya hay una transferencia en curso", severity="warning")
            return
        paths = [posixpath.join(source.location.path, entry.name) for entry in entries]
        dest = target.location.path
        if not source.location.host and not target.location.host and any(os.path.abspath(path) == os.path.abspath(os.path.join(dest, entry.name)) for path, entry in zip(paths, entries)):
            self.notify("Origen y destino son el mismo archivo", severity="error")
            return
        names = "\n".join(entry.name for entry in entries[:10])
        if len(entries) > 10:
            names += f"\n… y {len(entries) - 10} más"
        if not await self.push_screen_wait(Confirm(f"{method.upper()}: {len(entries)} elemento(s)\n{source.location.title} → {target.location.title}\n{names}\nPuede sobrescribir archivos existentes. ¿Continuar?")):
            return
        if self.transfer_active:
            self.notify("Ya hay una transferencia en curso", severity="warning")
            return
        self.transfer_active = True
        self.transfer_cancel_allowed = method in ("rsync", "scp")
        self.transfer_cancelled = False
        self.query_one("#status", Static).update(f"En curso: {method} ({len(entries)} elementos) · Ctrl+X: cancelar SCP/rsync")
        progress = self.query_one("#progress", ProgressBar)
        if method in ("rsync", "scp"):
            progress.update(progress=0)
            progress.display = True
        try:
            if method in ("move", "direct"):
                def local_operation() -> None:
                    for path, entry in zip(paths, entries):
                        if method == "move":
                            shutil.move(path, dest)
                        elif entry.directory:
                            shutil.copytree(path, os.path.join(dest, entry.name), dirs_exist_ok=True)
                        else:
                            shutil.copy2(path, dest)
                await asyncio.to_thread(local_operation)
            else:
                remote = source.location if source.location.host else target.location
                password = remote.password if remote.host else ""
                if method == "rsync":
                    source_args = [f"{source.location.host}:{path}" if source.location.host else path for path in paths]
                    dest_arg = f"{target.location.host}:{dest.rstrip('/')}/" if target.location.host else dest.rstrip('/') + "/"
                    transport = [*ssh_prefix(password), "ssh", *ssh_options(password)]
                    args = ["rsync", "-a", "-s", "--info=progress2", "-e", shlex.join(transport), "--", *source_args, dest_arg]
                else:
                    # SFTP mode in modern scp accepts spaces in remote paths without shell quoting.
                    source_args = [f"{source.location.host}:{path}" if source.location.host else path for path in paths]
                    dest_arg = f"{target.location.host}:{dest.rstrip('/')}/" if target.location.host else dest.rstrip('/') + "/"
                    args = [*ssh_prefix(password), "scp", "-r", *ssh_options(password), "--", *source_args, dest_arg]
                def report(percent: int, detail: str) -> None:
                    progress.update(progress=percent)
                    label = "global" if method == "rsync" else "archivo actual"
                    self.query_one("#status", Static).update(f"{method.upper()} ({label}): {detail} · Ctrl+X: cancelar")
                def started(process) -> None:
                    self.transfer_process = process
                    if self.transfer_cancelled:
                        self.action_cancel_transfer()
                code, output = await progress_command(*args, password=password, on_progress=report, on_start=started)
                if self.transfer_cancelled:
                    self.query_one("#status", Static).update("Transferencia cancelada; pueden quedar archivos parciales")
                    self.notify("Transferencia cancelada", severity="warning")
                    return
                if code:
                    raise RuntimeError(output.strip() or f"Error de {method}: {code}")
            source.marked.difference_update(entry.name for entry in entries)
            self.query_one("#status", Static).update(f"Completado: {method}, {len(entries)} elemento(s)")
            self.notify("Operación completada")
        except (OSError, shutil.Error, RuntimeError) as exc:
            self.query_one("#status", Static).update("Error en la operación")
            self.notify(str(exc), severity="error", timeout=15)
        finally:
            self.transfer_process = None
            self.transfer_active = False
            self.transfer_cancel_allowed = False
            progress.display = False
            await self.action_refresh_panels()


if __name__ == "__main__":
    Commander().run()
