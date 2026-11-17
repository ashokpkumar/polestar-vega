__version__ = "0.1.0"


def __getattr__(name):
    # lazy, so `import flowmap` stays light and the CLI never needs the web stack
    if name == "mount_flowmap":
        from .mount import mount_flowmap

        return mount_flowmap
    raise AttributeError(name)
