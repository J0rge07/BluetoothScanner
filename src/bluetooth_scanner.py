"""
bluetooth_scanner.py
====================================
A graphical Bluetooth Low Energy (BLE) scanner and GATT explorer.

The program continuously scans for nearby BLE devices, refreshes the list
every 2 seconds sorted from strongest to weakest signal (RSSI), and lets the
user connect to a device, browse its services and characteristics, and
read, write, or subscribe to notifications on a characteristic.

The GUI is built with ``tkinter`` and the Bluetooth work is done with
``bleak``. Because ``bleak`` is ``asyncio`` based and ``tkinter`` has its own
blocking main loop, the ``asyncio`` event loop runs in a background thread.
Results are passed back to the GUI thread through a thread-safe queue so
that tkinter widgets are only ever touched from the main thread.

| Author: Jorge Hernandez Umanzor
| Date: 2026 November 10
"""

import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from bleak import BleakClient, BleakScanner

#: How often the device list is redrawn, in milliseconds.
REFRESH_MS = 2000
#: Devices not heard from in this many seconds are dropped from the list.
STALE_SECONDS = 15.0
#: Maximum time to wait for a connection before giving up, in seconds.
CONNECT_TIMEOUT = 20.0


def format_value(data):
    """
    Format raw characteristic bytes into a readable string.

    Parameters
    ----------
    data : bytes or bytearray
        The raw value read from (or notified by) a characteristic.

    Returns
    -------
    str
        The value as hex, plus UTF-8 text if printable and an unsigned
        little-endian integer if the value is 8 bytes or shorter.

    Examples
    --------
    >>> format_value(b'Hi')
    "hex: 48 69 | text: 'Hi' | int: 26952"
    >>> format_value(bytes([100]))
    "hex: 64 | text: 'd' | int: 100"
    """
    data = bytes(data)
    if not data:
        return "(empty)"
    parts = ["hex: " + data.hex(" ")]
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            parts.append(f"text: {text!r}")
    except UnicodeDecodeError:
        pass
    if len(data) <= 8:
        parts.append(f"int: {int.from_bytes(data, 'little')}")
    return " | ".join(parts)


def parse_input(text, mode):
    """
    Convert user input into bytes to write to a characteristic.

    Parameters
    ----------
    text : str
        The text the user typed.
    mode : str
        ``"Hex"`` to interpret ``text`` as hex digits (spaces allowed),
        anything else to encode it as UTF-8.

    Returns
    -------
    bytes
        The bytes to write.

    Raises
    ------
    ValueError
        If ``mode`` is ``"Hex"`` and ``text`` is not valid hex.

    Examples
    --------
    >>> parse_input('01 ff', 'Hex')
    b'\\x01\\xff'
    >>> parse_input('hi', 'Text (UTF-8)')
    b'hi'
    """
    if mode == "Hex":
        return bytes.fromhex(text.replace("0x", "").replace(",", " "))
    return text.encode("utf-8")


def sort_devices(devices):
    """
    Sort scanned devices from strongest to weakest signal.

    Parameters
    ----------
    devices : dict
        Maps address to a dict with at least an ``"rssi"`` key.

    Returns
    -------
    list of str
        Addresses ordered by RSSI, strongest (closest to 0) first.

    Examples
    --------
    >>> sort_devices({'A': {'rssi': -80}, 'B': {'rssi': -40}, 'C': {'rssi': -60}})
    ['B', 'C', 'A']
    """
    return sorted(devices, key=lambda a: devices[a]["rssi"], reverse=True)


class AsyncioThread:
    """
    Runs an ``asyncio`` event loop in a daemon background thread.

    Attributes
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop running in the background thread.
    """

    def __init__(self):
        """Create the loop and start the background thread."""
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Thread target: run the event loop forever."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        """
        Schedule a coroutine on the background loop.

        Parameters
        ----------
        coro : coroutine
            The coroutine to run.

        Returns
        -------
        concurrent.futures.Future
            A future that can be checked or cancelled from any thread.
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        """Stop the background event loop."""
        self.loop.call_soon_threadsafe(self.loop.stop)


class BluetoothScannerApp:
    """
    The main tkinter application window.

    Parameters
    ----------
    root : tk.Tk
        The tkinter root window.

    Attributes
    ----------
    devices : dict
        Maps device address to a dict with keys ``device``, ``name``,
        ``rssi`` and ``last_seen``.
    client : bleak.BleakClient or None
        The connected client, or ``None`` when not connected.
    """

    def __init__(self, root):
        """
        Build the GUI and start scanning.

        Parameters
        ----------
        root : tk.Tk
            The tkinter root window.
        """
        self.root = root
        self.aio = AsyncioThread()
        self.events = queue.Queue()  # callables to run on the GUI thread

        self.devices = {}
        self.devices_lock = threading.Lock()
        self.scanner = None
        self.scanning = False

        self.client = None
        self.connected_address = None
        self.connect_future = None
        self.services = {}         # tree iid -> BleakGATTService
        self.characteristics = {}  # tree iid -> BleakGATTCharacteristic
        self.notifying = set()     # handles of characteristics with notify on

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._pump_events)
        self.root.after(REFRESH_MS, self._refresh_device_list)
        self.start_scan()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        """Create and lay out all widgets."""
        self.root.title("Bluetooth LE Scanner")
        self.root.geometry("1150x720")
        self.root.minsize(900, 560)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ----- left: device list
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        controls = ttk.Frame(left)
        controls.pack(fill=tk.X)
        self.scan_btn = ttk.Button(controls, text="Stop Scan", command=self.toggle_scan)
        self.scan_btn.pack(side=tk.LEFT)
        self.hide_unnamed = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Hide unnamed", variable=self.hide_unnamed,
                        command=self._refresh_now).pack(side=tk.LEFT, padx=8)
        self.count_lbl = ttk.Label(controls, text="0 devices")
        self.count_lbl.pack(side=tk.RIGHT)

        dev_frame = ttk.Frame(left)
        dev_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 6))
        self.dev_tree = ttk.Treeview(dev_frame, columns=("name", "address", "rssi"),
                                     show="headings", selectmode="browse")
        for col, text, width in (("name", "Name", 180), ("address", "Address", 250),
                                 ("rssi", "RSSI (dBm)", 80)):
            self.dev_tree.heading(col, text=text)
            self.dev_tree.column(col, width=width, anchor=tk.W if col != "rssi" else tk.CENTER)
        self.dev_tree.tag_configure("connected", background="#cdeccd")
        self.dev_tree.tag_configure("stale", foreground="#999999")
        dev_scroll = ttk.Scrollbar(dev_frame, orient=tk.VERTICAL, command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=dev_scroll.set)
        self.dev_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dev_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.dev_tree.bind("<Double-1>", lambda e: self.connect())

        conn = ttk.Frame(left)
        conn.pack(fill=tk.X)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.connect)
        self.cancel_btn = ttk.Button(conn, text="Cancel", command=self.cancel_connect,
                                     state=tk.DISABLED)
        self.disconnect_btn = ttk.Button(conn, text="Disconnect", command=self.disconnect,
                                         state=tk.DISABLED)
        for b in (self.connect_btn, self.cancel_btn, self.disconnect_btn):
            b.pack(side=tk.LEFT, padx=(0, 6))
        self.status_lbl = ttk.Label(left, text="Not connected", foreground="#555555")
        self.status_lbl.pack(fill=tk.X, pady=(6, 0))

        # ----- right: services / characteristics
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        ttk.Label(right, text="Services").pack(anchor=tk.W)
        self.svc_tree = ttk.Treeview(right, columns=("uuid", "desc"), show="headings",
                                     selectmode="browse", height=7)
        self.svc_tree.heading("uuid", text="UUID")
        self.svc_tree.heading("desc", text="Description")
        self.svc_tree.column("uuid", width=270)
        self.svc_tree.column("desc", width=200)
        self.svc_tree.pack(fill=tk.BOTH, expand=True)
        self.svc_tree.bind("<<TreeviewSelect>>", self.on_service_selected)

        ttk.Label(right, text="Characteristics").pack(anchor=tk.W, pady=(8, 0))
        self.chr_tree = ttk.Treeview(right, columns=("uuid", "desc", "props"),
                                     show="headings", selectmode="browse", height=7)
        self.chr_tree.heading("uuid", text="UUID")
        self.chr_tree.heading("desc", text="Description")
        self.chr_tree.heading("props", text="Properties")
        self.chr_tree.column("uuid", width=270)
        self.chr_tree.column("desc", width=150)
        self.chr_tree.column("props", width=170)
        self.chr_tree.pack(fill=tk.BOTH, expand=True)
        self.chr_tree.bind("<<TreeviewSelect>>", self.on_characteristic_selected)

        actions = ttk.Frame(right)
        actions.pack(fill=tk.X, pady=(6, 0))
        self.read_btn = ttk.Button(actions, text="Read", command=self.read_char,
                                   state=tk.DISABLED)
        self.read_btn.pack(side=tk.LEFT)
        self.notify_btn = ttk.Button(actions, text="Start Notify", command=self.toggle_notify,
                                     state=tk.DISABLED)
        self.notify_btn.pack(side=tk.LEFT, padx=6)
        self.write_entry = ttk.Entry(actions, width=22)
        self.write_entry.pack(side=tk.LEFT, padx=(12, 4), fill=tk.X, expand=True)
        self.write_mode = ttk.Combobox(actions, values=("Text (UTF-8)", "Hex"),
                                       width=11, state="readonly")
        self.write_mode.current(0)
        self.write_mode.pack(side=tk.LEFT)
        self.write_btn = ttk.Button(actions, text="Write", command=self.write_char,
                                    state=tk.DISABLED)
        self.write_btn.pack(side=tk.LEFT, padx=(4, 0))

        self.value_lbl = ttk.Label(right, text="Value: -", font="TkFixedFont")
        self.value_lbl.pack(fill=tk.X, pady=(6, 0))

        # ----- bottom: log
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=8, state=tk.DISABLED, wrap=tk.WORD)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def log(self, message):
        """
        Append a timestamped line to the log box (GUI thread only).

        Parameters
        ----------
        message : str
            The message to log.
        """
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def post(self, func, *args):
        """
        Queue a function to run on the GUI thread (safe from any thread).

        Parameters
        ----------
        func : callable
            The function to call.
        *args
            Arguments for ``func``.
        """
        self.events.put((func, args))

    def _pump_events(self):
        """Run queued GUI callbacks posted by the background thread."""
        try:
            while True:
                func, args = self.events.get_nowait()
                func(*args)
        except queue.Empty:
            pass
        self.root.after(100, self._pump_events)

    def _run_async(self, coro, on_error_prefix):
        """
        Run a coroutine in the background and log any exception it raises.

        Parameters
        ----------
        coro : coroutine
            The coroutine to run.
        on_error_prefix : str
            Text placed before the error message in the log.

        Returns
        -------
        concurrent.futures.Future
            The future for the scheduled coroutine.
        """
        future = self.aio.submit(coro)

        def done(f):
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                self.post(self.log, f"{on_error_prefix}: {exc!r}")
        future.add_done_callback(done)
        return future

    # ------------------------------------------------------------ scanning
    def _on_advertisement(self, device, adv):
        """
        Bleak detection callback (runs in the background thread).

        Parameters
        ----------
        device : bleak.backends.device.BLEDevice
            The device that advertised.
        adv : bleak.backends.scanner.AdvertisementData
            The advertisement data, including RSSI.
        """
        with self.devices_lock:
            old = self.devices.get(device.address, {})
            self.devices[device.address] = {
                "device": device,
                "name": adv.local_name or device.name or old.get("name") or "(unknown)",
                "rssi": adv.rssi,
                "last_seen": time.time(),
            }

    async def _start_scanner(self):
        """Create and start the bleak scanner."""
        if self.scanner is None:
            self.scanner = BleakScanner(detection_callback=self._on_advertisement)
        await self.scanner.start()

    async def _stop_scanner(self):
        """Stop the bleak scanner if it is running."""
        if self.scanner is not None:
            await self.scanner.stop()

    def start_scan(self):
        """Start scanning for devices."""
        self.scanning = True
        self.scan_btn.configure(text="Stop Scan")
        self._run_async(self._start_scanner(), "Scan error")
        self.log("Scanning started")

    def stop_scan(self):
        """Stop scanning for devices."""
        self.scanning = False
        self.scan_btn.configure(text="Start Scan")
        self._run_async(self._stop_scanner(), "Stop scan error")
        self.log("Scanning stopped")

    def toggle_scan(self):
        """Start or stop scanning depending on the current state."""
        if self.scanning:
            self.stop_scan()
        else:
            self.start_scan()

    def _refresh_device_list(self):
        """Redraw the device list, then schedule itself again in 2 seconds."""
        self._refresh_now()
        self.root.after(REFRESH_MS, self._refresh_device_list)

    def _refresh_now(self):
        """
        Update the device Treeview sorted by RSSI.

        Rows use the device address as their item id and are reordered with
        ``Treeview.move`` rather than deleted and reinserted, so the user's
        selection follows the device as it moves around the list.
        """
        now = time.time()
        selected = self.dev_tree.selection()
        keep = set(selected)
        if self.connected_address:
            keep.add(self.connected_address)

        with self.devices_lock:
            for addr in [a for a, d in self.devices.items()
                         if now - d["last_seen"] > STALE_SECONDS and a not in keep]:
                del self.devices[addr]
            snapshot = {a: dict(d) for a, d in self.devices.items()}

        if self.hide_unnamed.get():
            snapshot = {a: d for a, d in snapshot.items()
                        if d["name"] != "(unknown)" or a in keep}

        order = sort_devices(snapshot)
        for addr in set(self.dev_tree.get_children()) - set(order):
            self.dev_tree.delete(addr)

        for index, addr in enumerate(order):
            d = snapshot[addr]
            tags = []
            if addr == self.connected_address:
                tags.append("connected")
            elif now - d["last_seen"] > STALE_SECONDS:
                tags.append("stale")
            values = (d["name"], addr, d["rssi"])
            if self.dev_tree.exists(addr):
                self.dev_tree.item(addr, values=values, tags=tags)
                self.dev_tree.move(addr, "", index)
            else:
                self.dev_tree.insert("", index, iid=addr, values=values, tags=tags)

        if selected and self.dev_tree.exists(selected[0]):
            self.dev_tree.selection_set(selected[0])
        self.count_lbl.configure(text=f"{len(order)} devices")

    # ---------------------------------------------------------- connection
    def connect(self):
        """Connect to the device selected in the list."""
        sel = self.dev_tree.selection()
        if not sel:
            messagebox.showinfo("Connect", "Select a device first.")
            return
        if self.client is not None or self.connect_future is not None:
            messagebox.showinfo("Connect", "Disconnect from the current device first.")
            return
        address = sel[0]
        with self.devices_lock:
            entry = self.devices.get(address)
        if entry is None:
            return
        self._set_status(f"Connecting to {entry['name']} ...")
        self.connect_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.log(f"Connecting to {entry['name']} ({address})")
        future = self.aio.submit(self._connect(entry["device"], entry["name"]))
        self.connect_future = future

        def on_done(f):
            if f.cancelled():
                self.post(self._connect_finished, None, address, "Connection cancelled")
        future.add_done_callback(on_done)

    async def _connect(self, device, name):
        """
        Connect to a device and load its services (background thread).

        Parameters
        ----------
        device : bleak.backends.device.BLEDevice
            The device to connect to.
        name : str
            The display name, for log messages.
        """
        client = BleakClient(device, disconnected_callback=self._on_disconnected,
                             timeout=CONNECT_TIMEOUT)
        try:
            await client.connect()
        except asyncio.CancelledError:
            # The GUI is told about the cancel by the future's done callback.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        except Exception as exc:
            self.post(self._connect_finished, None, device.address,
                      f"Connection to {name} failed: {exc!r}")
            return
        self.post(self._connect_finished, client, device.address, f"Connected to {name}")

    def _connect_finished(self, client, address, message):
        """
        Update the GUI after a connection attempt ends (GUI thread).

        Parameters
        ----------
        client : bleak.BleakClient or None
            The connected client, or ``None`` if the attempt failed.
        address : str
            Address of the device.
        message : str
            Message to log and show in the status line.
        """
        self.connect_future = None
        self.cancel_btn.configure(state=tk.DISABLED)
        self.log(message)
        if client is None:
            self.connect_btn.configure(state=tk.NORMAL)
            self._set_status("Not connected")
            return
        self.client = client
        self.connected_address = address
        self.disconnect_btn.configure(state=tk.NORMAL)
        self._set_status(message)
        self._populate_services()
        self._refresh_now()

    def cancel_connect(self):
        """Cancel a connection attempt that is in progress."""
        if self.connect_future is not None:
            self.log("Cancelling connection attempt ...")
            self.connect_future.cancel()

    def disconnect(self):
        """Disconnect from the connected device."""
        if self.client is None:
            return
        self.log("Disconnecting ...")
        self._run_async(self.client.disconnect(), "Disconnect error")

    def _on_disconnected(self, client):
        """
        Bleak callback when the device disconnects (background thread).

        Parameters
        ----------
        client : bleak.BleakClient
            The client that disconnected.
        """
        self.post(self._handle_disconnected, client)

    def _handle_disconnected(self, client):
        """
        Reset the GUI after a disconnect (GUI thread).

        Parameters
        ----------
        client : bleak.BleakClient
            The client that disconnected.
        """
        if client is not self.client:
            return  # e.g. a cancelled attempt that never fully connected
        self.log("Disconnected")
        self.client = None
        self.connected_address = None
        self.notifying.clear()
        self.services.clear()
        self.characteristics.clear()
        self.svc_tree.delete(*self.svc_tree.get_children())
        self.chr_tree.delete(*self.chr_tree.get_children())
        self.value_lbl.configure(text="Value: -")
        self.connect_btn.configure(state=tk.NORMAL)
        self.disconnect_btn.configure(state=tk.DISABLED)
        self._set_char_buttons(None)
        self._set_status("Not connected")
        self._refresh_now()

    def _set_status(self, text):
        """
        Set the status line text.

        Parameters
        ----------
        text : str
            The new status text.
        """
        self.status_lbl.configure(text=text)

    # ------------------------------------------- services / characteristics
    def _populate_services(self):
        """Fill the services list from the connected client."""
        self.svc_tree.delete(*self.svc_tree.get_children())
        self.services.clear()
        for service in self.client.services:
            iid = f"svc-{service.handle}"
            self.services[iid] = service
            self.svc_tree.insert("", tk.END, iid=iid,
                                 values=(service.uuid, service.description))
        self.log(f"Found {len(self.services)} services")

    def on_service_selected(self, _event=None):
        """Show the characteristics of the selected service."""
        sel = self.svc_tree.selection()
        self.chr_tree.delete(*self.chr_tree.get_children())
        self.characteristics.clear()
        self._set_char_buttons(None)
        if not sel:
            return
        for char in self.services[sel[0]].characteristics:
            iid = f"chr-{char.handle}"
            self.characteristics[iid] = char
            self.chr_tree.insert("", tk.END, iid=iid,
                                 values=(char.uuid, char.description,
                                         ", ".join(char.properties)))

    def selected_characteristic(self):
        """
        Return the selected characteristic.

        Returns
        -------
        bleak.backends.characteristic.BleakGATTCharacteristic or None
            The selected characteristic, or ``None``.
        """
        sel = self.chr_tree.selection()
        return self.characteristics.get(sel[0]) if sel else None

    def on_characteristic_selected(self, _event=None):
        """Enable the buttons that the selected characteristic supports."""
        self._set_char_buttons(self.selected_characteristic())

    def _set_char_buttons(self, char):
        """
        Enable or disable Read/Write/Notify based on characteristic properties.

        Parameters
        ----------
        char : BleakGATTCharacteristic or None
            The selected characteristic, or ``None`` to disable all.
        """
        props = set(char.properties) if char is not None else set()
        on = lambda flag: tk.NORMAL if flag else tk.DISABLED
        self.read_btn.configure(state=on("read" in props))
        self.write_btn.configure(
            state=on(bool(props & {"write", "write-without-response"})))
        self.notify_btn.configure(state=on(bool(props & {"notify", "indicate"})))
        notifying = char is not None and char.handle in self.notifying
        self.notify_btn.configure(text="Stop Notify" if notifying else "Start Notify")

    def read_char(self):
        """Read the selected characteristic."""
        char = self.selected_characteristic()
        if char is None or self.client is None:
            return

        async def do_read():
            data = await self.client.read_gatt_char(char)
            self.post(self._show_value, char, data, "Read")
        self._run_async(do_read(), f"Read {char.uuid} failed")

    def write_char(self):
        """Write the entry text to the selected characteristic."""
        char = self.selected_characteristic()
        if char is None or self.client is None:
            return
        try:
            data = parse_input(self.write_entry.get(), self.write_mode.get())
        except ValueError:
            messagebox.showerror("Write", "Invalid hex. Example: 01 A0 FF")
            return
        response = "write" in char.properties

        async def do_write():
            await self.client.write_gatt_char(char, data, response=response)
            self.post(self.log, f"Wrote {format_value(data)} to {char.uuid}")
        self._run_async(do_write(), f"Write {char.uuid} failed")

    def toggle_notify(self):
        """Start or stop notifications on the selected characteristic."""
        char = self.selected_characteristic()
        if char is None or self.client is None:
            return
        client = self.client

        def on_notify(_sender, data):
            self.post(self._show_value, char, data, "Notify")

        if char.handle in self.notifying:
            async def stop():
                await client.stop_notify(char)
                self.post(self._notify_changed, char, False)
            self._run_async(stop(), f"Stop notify {char.uuid} failed")
        else:
            async def start():
                await client.start_notify(char, on_notify)
                self.post(self._notify_changed, char, True)
            self._run_async(start(), f"Start notify {char.uuid} failed")

    def _notify_changed(self, char, enabled):
        """
        Record the notify state of a characteristic (GUI thread).

        Parameters
        ----------
        char : BleakGATTCharacteristic
            The characteristic.
        enabled : bool
            ``True`` if notifications were started.
        """
        if enabled:
            self.notifying.add(char.handle)
        else:
            self.notifying.discard(char.handle)
        self.log(f"Notifications {'on' if enabled else 'off'} for {char.uuid}")
        self._set_char_buttons(self.selected_characteristic())

    def _show_value(self, char, data, source):
        """
        Display a value read or notified from a characteristic (GUI thread).

        Parameters
        ----------
        char : BleakGATTCharacteristic
            The characteristic the value came from.
        data : bytes or bytearray
            The raw value.
        source : str
            ``"Read"`` or ``"Notify"``.
        """
        text = format_value(data)
        if char is self.selected_characteristic():
            self.value_lbl.configure(text=f"Value: {text}")
        self.log(f"{source} {char.description} ({char.uuid}): {text}")

    # ------------------------------------------------------------ shutdown
    def on_close(self):
        """Disconnect, stop scanning and close the window."""
        async def shutdown():
            if self.client is not None:
                await self.client.disconnect()
            await self._stop_scanner()
        future = self.aio.submit(shutdown())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.aio.stop()
        self.root.destroy()


def main():
    """Create the window and run the application."""
    root = tk.Tk()
    BluetoothScannerApp(root)
    root.mainloop()


if __name__ == '__main__':
    """Runs if file called as script as opposed to being imported as a library
    """
    main()
