import sys

if __name__ == "__main__":
    try:
        from pyvised.app import main
    except BaseException:
        # e.g. a library missing from the installed app: keep the traceback in the log file
        import logging

        from pyvised.core.logging_setup import configure_logging

        configure_logging()
        logging.getLogger("pyvised").exception("PyVised could not start")
        raise
    sys.exit(main())
