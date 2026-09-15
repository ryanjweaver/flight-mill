"""PyInstaller entry point; package imports stay identical to source launches."""

from flightmill.desktop.packaged import main

if __name__ == "__main__":
    main()
