from contextlib import ExitStack

from . import installer, settings, upstreams
from .common import APPS, Problem, atomic, child


def current(root, book, name):
    installed = installer.install_location(root, name)
    route = book.route(name)
    return bool(installed and route
                and installed[0].get("lock_sha256") == route["lock_sha256"]
                and installed[0].get("upstream_version") == route["upstream_version"]
                and child(root, "apps", name, "launch", "native.py").is_file())


def install_one(root, book, uv, config_lock, name, job, batch=False):
    def prepare(directory, publish):
        with config_lock, ExitStack() as rollback:
            config = settings.load(root)
            launcher = upstreams.standalone_path(root, name)
            if launcher.is_file():
                rollback.callback(atomic, launcher, launcher.read_bytes())
            else:
                rollback.callback(launcher.unlink, missing_ok=True)
            if settings.applied(root, name) is None:
                try:
                    projection = settings.resolve(config, name, book.capabilities(name))
                except Problem:
                    # Keep a usable native entry when one app has an invalid guided setting.
                    projection = settings.resolve(settings.parse(""), name, book.capabilities(name))
                rollback.callback(child(launcher.parent, "profile.json").unlink, missing_ok=True)
                settings.install_projection(root, name, projection)
            upstreams.prepare_launch(root, name, directory)
            publish()
            rollback.pop_all()

    def checkpoint(final=False):
        job.checkpoint(final=final and not batch)

    def step(message):
        job.report(name, "Running", message)

    directory = installer.install(root, book, name, uv, checkpoint, prepare, step)
    job.report(name, "Installed", "Native launcher prepared")
    return directory


def install_core(root, book, uv, config_lock, job, browser_alive=lambda name: False):
    rows = {name: book.route(name) for name in APPS}
    for name in APPS:
        job.report(name, "Queued", "Waiting for setup")
    for name, route in rows.items():
        job.checkpoint()
        if current(root, book, name):
            job.report(name, "Installed", "Current installation kept")
        elif not route or browser_alive(name):
            job.report(name, "Unavailable", "No bundle for this host" if not route else "Stop this app then retry")
        else:
            try:
                install_one(root, book, uv, config_lock, name, job, batch=True)
            except Problem as error:
                if error.code == "cancelled":
                    job.report(name, "Cancelled", "Previous files kept")
                    raise
                job.report(name, "Failed", str(error))
            except (OSError, ValueError, TypeError, KeyError):
                job.report(name, "Failed", "Could not prepare this app. See docs/releases.md, then retry.")
    job.checkpoint(final=True)
    return {"complete": all(current(root, book, name) for name in APPS)}

