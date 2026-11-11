import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ["--probe-engine"]:
        from yt_downloader.worker import probe_engine
        return probe_engine(*args[1:])
    if args[:1] == ["--worker"]:
        from yt_downloader.worker import run_worker
        return run_worker(*args[1:])
    if args[:1] == ["--prepare-seed"]:
        from pathlib import Path
        from yt_downloader.updates.manager import COMPONENTS, EngineManager
        manager = EngineManager(Path(args[1]), print)
        manager.diagnose()
        for component in COMPONENTS:
            manager.check(component)
        return 0 if manager.snapshot().ready else 1
    from yt_downloader.ui import launch
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
