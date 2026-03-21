try:
    from importlib.metadata import version

    __version__ = version("whatsapp-ctx-cli")
except Exception:
    __version__ = "0.0.0"
