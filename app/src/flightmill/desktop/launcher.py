"""Launch the same instrument service in a desktop window or local browser."""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from flightmill.api.app import create_app
from flightmill.desktop.lifecycle import DesktopLifecycle


class SessionTokenFilter(logging.Filter):
    """Do not persist ephemeral WebSocket authorization in diagnostic logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(r"([?&]token=)[^&\s\"']+", r"\1[redacted]", record.getMessage())
        record.args = ()
        return True


def _listener(port: int | None) -> socket.socket:
    """Reserve a loopback port through server shutdown, including default fallback."""
    for candidate in [port] if port is not None else range(8765, 8796):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", candidate))
            return listener
        except OSError:
            listener.close()
            if port is not None:
                raise
    raise OSError("No available local application port between 8765 and 8795")


def main() -> None:
    parser = argparse.ArgumentParser(description="Flight Mill acquisition workspace")
    parser.add_argument("--browser", action="store_true", help="Open in the local browser")
    parser.add_argument("--no-open", action="store_true", help="Serve without opening a window")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "Documents" / "FlightMill" / "Simulations")
    args = parser.parse_args()
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("Choose a port from 1024 to 65535.")
    try:
        listener = _listener(args.port)
        args.port = listener.getsockname()[1]
    except OSError as exc:
        parser.error(f"The requested local port is already in use or unavailable: {exc}")
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(args.output_dir / "application.log", maxBytes=2_000_000,
                                 backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SessionTokenFilter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    app = create_app(args.output_dir)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                          workers=1, access_log=False, log_config=None))
    url = f"http://127.0.0.1:{args.port}"
    logging.info("Flight Mill local application: %s", url)
    if args.no_open:
        try:
            server.run(sockets=[listener])
        finally:
            listener.close()
        return

    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]},
                              name="flightmill-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("The local recording service could not start. See application.log.")

    def browser_mode() -> None:
        webbrowser.open(url)
        try:
            while thread.is_alive():
                thread.join(timeout=0.5)
        except KeyboardInterrupt:
            server.should_exit = True
            thread.join(timeout=5)

    if args.browser:
        try:
            browser_mode()
        finally:
            listener.close()
        return

    try:
        import webview

        service = app.state.acquisition

        class DesktopBridge:
            def choose_output_dir(self):
                result = window.create_file_dialog(webview.FileDialog.FOLDER)
                return result[0] if result else None

            def open_output_folder(self):
                path = Path(service.snapshot()["output_dir"]).resolve()
                if not path.is_dir():
                    raise ValueError("The selected output folder is unavailable. Check its location "
                                     "in Trial setup; existing recordings have not been moved.")
                if os.name != "nt":
                    raise ValueError("Open the selected folder using your file manager.")
                try:
                    os.startfile(path)
                except OSError as exc:
                    raise ValueError("Windows could not open the selected output folder. "
                                     "Check its availability and permissions.") from exc
                return True

        # pywebview 6.0 Edge uses its native SaveFileDialog for registered HTTP
        # attachments. Keep the API cookie/origin checks and fixed artifact routes.
        webview.settings['ALLOW_DOWNLOADS'] = True
        window = webview.create_window("Flight Mill · Acquisition", url,
                                       width=1440, height=940, min_size=(980, 680),
                                       background_color="#10161D", js_api=DesktopBridge())

        lifecycle = DesktopLifecycle(service, window)
        window.events.closing += lifecycle.closing
        webview.start(gui="edgechromium")
    except Exception:
        logging.exception("Desktop renderer unavailable; opening the local browser")
        browser_mode()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


if __name__ == "__main__":
    main()
