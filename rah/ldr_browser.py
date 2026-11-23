"""Run the pinned ldr-web entry with authenticated RAH lifecycle controls."""
import os

from gptr_browser import LocalBrowser


def main():
    import uvicorn
    from local_deep_research.web.app import main as native_main

    token = os.environ.pop("RAH_LDR_STARTUP_TOKEN")
    hub = os.environ.pop("RAH_HANDOFF_HUB", "")
    native_run = uvicorn.run

    def managed_run(app, **options):
        config = uvicorn.Config(app, **options)
        config.load()
        server = uvicorn.Server(config)

        def stop():
            server.should_exit = True

        native = config.loaded_app
        if hub:
            from native_handoff import NativeHandoff
            native = NativeHandoff(native, "ldr", config.port, token, hub)
        config.loaded_app = LocalBrowser(native, config.port, token, stop, guard_native=False)
        server.run()

    # Keep the native entry's startup, configuration, and lifespan; wrap its single listener.
    uvicorn.run = managed_run
    try:
        native_main()
    finally:
        uvicorn.run = native_run


if __name__ == "__main__":
    main()