"""
Single entry point: the bot (mail polling) in a background thread plus the web
panel (uvicorn).

With the panel switched off (ADMIN_PANEL_ENABLED=false) only the bot runs, which
is equivalent to `python email_bot.py`.
"""
import signal
import threading
import logging

import config
import tariffs

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flag for a clean shutdown
_stop_event = threading.Event()


def run_bot_loop():
    """The mail polling loop, running in a background thread."""
    import email_bot

    logger.info("Checking the connection to 3x-ui...")
    from xui_client import get_shared_client
    xui = get_shared_client()
    if xui.login():
        logger.info("Connection to 3x-ui: OK.")
    else:
        logger.error("Connection to 3x-ui: FAILED. The bot is running, but requests to the panel may fail.")

    logger.info(f"Bot started. Mail poll interval: {config.POLL_INTERVAL_SECONDS} seconds.")
    known = tariffs.all_tariffs()
    if known:
        described = []
        for tariff in known:
            words = [c["word"] for c in tariffs.codes_for(tariff["id"]) if c["enabled"]]
            described.append(f"{tariff['name']} ({', '.join(words) if words else 'no word'})")
        logger.info(f"Tariffs: {len(known)} — " + "; ".join(described))
    else:
        logger.error("No tariffs are set up; registration will be refused until one exists.")

    while not _stop_event.is_set():
        try:
            email_bot.check_mail()
        except Exception as e:
            logger.error(f"Error in the bot loop: {e}", exc_info=True)
        # Wait in a way that can be cut short by a signal
        _stop_event.wait(config.POLL_INTERVAL_SECONDS)

    logger.info("Mail polling loop stopped.")


def main():
    # settings.json is laid over config before the bot and the panel start
    import settings
    import email_texts
    settings.load()
    email_texts.load()
    # After the settings, since a first run builds the opening tariff from them.
    tariffs.load()

    # Log collection into the buffer and file starts before the bot, so that
    # nothing is lost
    import applog
    applog.install()

    # The version check lives in its own thread and reports through the panel.
    import updater
    updater.start()

    # The bot runs in a daemon thread, so it dies with the main process
    bot_thread = threading.Thread(target=run_bot_loop, name="email-bot", daemon=True)
    bot_thread.start()

    # The server is created before the handlers are installed, so they can stop
    # it as well.
    server = None

    def _handle_signal(signum, frame):
        logger.info(f"Signal {signum} received, shutting down...")
        _stop_event.set()
        if server is not None:
            server.should_exit = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if config.ADMIN_PANEL_ENABLED:
        if not config.ADMIN_PANEL_PASSWORD:
            logger.error(
                "ADMIN_PANEL_ENABLED=true but ADMIN_PANEL_PASSWORD is unset. "
                "The panel will not start. Set a password in .env."
            )
        else:
            logger.info(
                f"Starting the web panel on http://{config.ADMIN_PANEL_HOST}:{config.ADMIN_PANEL_PORT} "
                f"(login: {config.ADMIN_PANEL_USER})"
            )
            import uvicorn
            from admin.app import app
            try:
                uvicorn_config = uvicorn.Config(
                    app,
                    host=config.ADMIN_PANEL_HOST,
                    port=config.ADMIN_PANEL_PORT,
                    log_level="info",
                )
                server = uvicorn.Server(uvicorn_config)
                # Keep our handlers: they stop both the bot and the server.
                server.install_signal_handlers = lambda: None
                server.run()
            except Exception as e:
                logger.error(f"Error in the web panel: {e}", exc_info=True)
    else:
        logger.info("Web panel disabled (ADMIN_PANEL_ENABLED=false). Running the bot alone.")
        # The bot is in a daemon thread; the main thread waits for the signal
        while not _stop_event.is_set():
            _stop_event.wait(1)

    _stop_event.set()
    bot_thread.join(timeout=5)
    logger.info("Finished.")


if __name__ == "__main__":
    main()
